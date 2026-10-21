"""
مدل‌های دیتابیس — چندمستأجری (multi-tenant).
"""
from datetime import datetime
from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, Numeric, String, Text,
    UniqueConstraint, func, select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Bot(Base):
    """یک ربات ساخته‌شده توسط یک کاربرِ بات‌ساز."""
    __tablename__ = "bots"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    token: Mapped[str] = mapped_column(String(120), unique=True)
    username: Mapped[str | None] = mapped_column(String(64))
    field: Mapped[str] = mapped_column(String(40), default="vpn_sales")
    plan: Mapped[str] = mapped_column(String(20), default="free")
    status: Mapped[str] = mapped_column(String(20), default="active")  # active/paused/locked
    # دلیلِ قفل‌شدنِ ربات توسط ادمینِ پلتفرم (وقتی status="locked")؛ همین متن
    # عیناً به کاربرانِ نهاییِ آن ربات نشان داده می‌شود.
    lock_reason: Mapped[str | None] = mapped_column(Text)
    # تاریخ سررسید پلن (تریال رایگان یا اشتراک پولی). null = بدون محدودیت
    plan_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    # تاریخ آخرین یادآوری سررسید (YYYY-MM-DD) تا روزی یک‌بار اطلاع داده شود
    last_expiry_notice: Mapped[str | None] = mapped_column(String(10))
    # تاریخ ورود به بایگانی (در صورت عدم پرداخت). null = در بایگانی نیست
    archived_at: Mapped[datetime | None] = mapped_column(DateTime)
    # دلیلِ بایگانی‌شدن: "nonpayment" (عدم تمدید) / "inactivity" (عدم فعالیت) /
    # "user_deleted" (خودِ مالک حذف کرده — این‌جوری هم به کاربر می‌گیم غیرقابل‌برگشته
    # هم یه بازهٔ کوتاه برای بازیابیِ دستی توسط ادمینِ پلتفرم نگه می‌داریم).
    # null یعنی یا بایگانی نیست یا (برای رکوردهای قدیمی‌تر از این ستون) نامشخص است.
    archive_reason: Mapped[str | None] = mapped_column(String(20))
    # آخرین آپدیتی که برای این ربات رسیده (پیام/کلیک از کاربر یا مالک)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime)
    # زمانی که اخطار «عدم فعالیت» برای مالک ارسال شد. null = اخطاری در جریان نیست
    inactivity_warned_at: Mapped[datetime | None] = mapped_column(DateTime)
    # بازهٔ فعلیِ اشتراکِ پولیِ ربات: ۱ یا ۲ (ماهه). برای ربات‌های رایگان معنا ندارد.
    sub_months: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # === PATCH_PLAN_UPGRADE_BANK_V1 ===
    # وقتی ربات از رشد به حرفه‌ای ارتقا می‌یابد، باقی‌ماندهٔ رشد (ثانیه) اینجا
    # نگه‌داری می‌شود و بعد از تمام‌شدنِ حرفه‌ای به رشد برمی‌گردد.
    banked_plan: Mapped[str | None] = mapped_column(String(20))
    banked_seconds: Mapped[int | None] = mapped_column(Integer)
    banked_sub_months: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # === PATCH_MINIAPP_V1 ===
    # آیا دکمهٔ «مینی‌اپ» (وب‌اپ خرید/پروفایل) برای این ربات فعال است؟ وقتی
    # مالک از پروفایلِ ربات در بات‌ساز این را روشن می‌کند، دکمهٔ منوی ربات
    # (Menu Button) به مینی‌اپ وصل می‌شود و کاربران نهایی می‌توانند بدون
    # رفتن به بک‌آفیس ربات، از همان‌جا خرید/پروفایل/سرویس‌ها را ببینند.
    miniapp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # === PATCH_PUBLIC_API_V1 ===
    # کلیدِ APIِ سطحِ ربات (نه هر کاربر جدا) — ادمین از پنلِ مدیریت (کنارِ
    # همون دامنهٔ اختصاصی) یه کلید می‌سازه و باهاش به همهٔ محصولات/سفارش‌ها/
    # کیف‌پولِ ربات دسترسیِ برنامه‌نویسی داره.
    api_key: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)


# === PATCH_CUSTOM_DOMAIN_V1 ===
class BotCustomDomain(Base):
    """دامنهٔ اختصاصیِ یک ربات — به‌جایِ دامنهٔ مشترکِ پلتفرم (WEBHOOK_BASE)،
    مالکِ ربات می‌تواند دامنهٔ خودش را برایِ لینک‌هایِ پرداخت/وب‌اپ/فایل
    استفاده کند. صدورِ گواهیِ SSL برایِ این دامنه به‌صورتِ خودکار و لحظه‌ایِ
    اولین درخواستِ HTTPS انجام می‌شود (on-demand TLS در Caddy) — ببین
    `deploy/Caddyfile` و `main_tenant.py`'s `/domain-ask`."""
    __tablename__ = "bot_custom_domains"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), unique=True, index=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    # مقداری که مالک باید به‌صورتِ رکوردِ TXT رویِ دامنه‌اش بگذارد تا مالکیتش
    # را ثابت کند، قبل از اینکه اجازه بدهیم Caddy برایش گواهی بگیرد.
    verify_token: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/verified/failed
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_check_note: Mapped[str | None] = mapped_column(String(300))


class BotUser(Base):
    """کاربر نهایی یک ربات."""
    __tablename__ = "bot_users"
    __table_args__ = (UniqueConstraint("bot_id", "telegram_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    full_name: Mapped[str | None] = mapped_column(String(120))
    username: Mapped[str | None] = mapped_column(String(64))
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    # === PATCH_KYC_V1 === احراز هویتِ سه‌سطحی
    #   0 = بدون احراز (سقفِ روزانهٔ پایین)
    #   1 = کارتِ بانکی تأییدشده (فقط با همان کارت، سقفِ بالاتر)
    #   2 = مدارکِ کامل (بدون محدودیت)
    kyc_level: Mapped[int] = mapped_column(Integer, default=0)
    kyc_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    kyc_card: Mapped[str | None] = mapped_column(String(32))
    kyc_note: Mapped[str | None] = mapped_column(String(255))
    spam_warnings: Mapped[int] = mapped_column(Integer, default=0)  # تعداد اخطارهای آنتی‌اسپم (۳ام = بن)
    balance: Mapped[float] = mapped_column(Numeric(12, 0), default=0)
    # === PATCH_PHONE_VERIFY_V1 ===
    # تاییدِ شماره‌همراه (ضدِ رفرالِ فیک) — فقط وقتی ادمینِ ربات این قابلیت
    # رو فعال کرده باشه، کاربر مجبوره قبل از استفاده از ربات، شماره‌شو با
    # دکمهٔ اشتراک‌گذاریِ مخاطبِ خودِ تلگرام تایید کنه.
    phone_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    phone_number: Mapped[str | None] = mapped_column(String(20))
    # === PATCH_PUBLIC_API_PER_USER_V1 ===
    # کلیدِ APIِ شخصیِ این کاربر — فقط وقتی api_access روشن باشه قابلِ‌ساختنه
    # (پیش‌فرض برای همه بسته است؛ ادمین دستی هرکی رو خواست باز می‌کنه).
    api_key: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    api_access: Mapped[bool] = mapped_column(Boolean, default=False)
    # === PATCH_API_PAID_ACCESS_V1 === پایانِ دسترسیِ API (None = دائمی/دستیِ ادمین)
    api_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    # درصدِ تخفیفِ اختصاصیِ همین کاربر رویِ خریدهایی که با کلیدِ خودش (نه
    # کلیدِ سطحِ ربات) از API انجام می‌ده — مثلِ یه نرخِ همکاریِ شخصی.
    api_discount_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    # === PATCH_BLOCKED_USERS_V1 ===
    # آیا کاربر ربات را بلاک کرده؟ تلگرام API‌ای برایِ پرسیدنِ این ندارد؛ تنها
    # راهِ فهمیدنش این است که هنگامِ ارسالِ پیام خطایِ Forbidden بگیریم. پس
    # این فیلد موقعِ ارسالِ همگانی/پیامِ اختصاصی به‌روز می‌شود و اگر کاربر
    # دوباره با ربات تعامل کند، خودکار پاک می‌شود.
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime)
    joined_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # === PATCH_VS_ONBOARDING_V1 ===
    # آیا کاربر «قوانینِ استفاده از ربات» را تایید کرده؟ فقط وقتی ادمین از
    # تنظیماتِ خدماتِ مجازی این را روشن کرده باشد پرسیده می‌شود.
    rules_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    rules_accepted_at: Mapped[datetime | None] = mapped_column(DateTime)
    # === PATCH_RULES_VERSION_V1 === نسخه (هشِ کوتاهِ متن) قوانینِ ربات که کاربر تأیید کرده؛ با تغییرِ قوانین دوباره پرسیده می‌شود
    rules_version: Mapped[str | None] = mapped_column(String(16))
    # === PATCH_NO_DOT_MSG_V1 === کیبوردِ پایینیِ قدیمی برایِ این کاربر یک‌بار پاک شده؟
    kb_cleared: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # === PATCH_BOT_LANG_TOGGLE_V1 ===
    # زبانِ ترجیحیِ کاربر برایِ نمایشِ متن‌هایِ ربات ("fa" پیش‌فرض، "en" اگر
    # دکمهٔ «تغییر زبان به انگلیسی» را زده باشد). ترجمه زنده و با هوشِ
    # مصنوعی انجام می‌شود، نه از پیش ذخیره‌شده.
    lang: Mapped[str] = mapped_column(String(5), default="fa")


class WalletTxn(Base):
    """تراکنش کیف پول."""
    __tablename__ = "wallet_txns"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    amount: Mapped[float] = mapped_column(Numeric(12, 0))
    kind: Mapped[str] = mapped_column(String(20))  # charge/deduct/purchase
    note: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# === PATCH_KYC_V1 ===
class KycRequest(Base):
    """درخواستِ احرازِ هویتِ کاربرِ رباتِ مستأجر."""
    __tablename__ = "kyc_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    level: Mapped[int] = mapped_column(Integer, default=1)      # 1 یا 2
    card: Mapped[str | None] = mapped_column(String(32))        # سطح ۱
    file_id: Mapped[str | None] = mapped_column(String(300))
    file_type: Mapped[str | None] = mapped_column(String(20))   # photo/video/video_note
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending/approved/rejected
    admin_note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # === PATCH_KYC_CARD_CHANGE_V1 === ""=احرازِ عادی، "card_change"=درخواستِ تغییرِ کارتِ ثبت‌شده
    kind: Mapped[str | None] = mapped_column(String(16), nullable=True)


# === PATCH_FILE_LINK_CONVERTER_V1 ===
# برای نوعِ ربات جدیدِ «تبدیل فایل↔لینک» (bot_field == "file_link"):
# هر فایلی که کاربر برایِ ربات بفرسته، رویِ دیسکِ سرور ذخیره می‌شه و یک
# کدِ کوتاهِ یکتا می‌گیره؛ لینکِ دانلودِ مستقیمِ HTTP از رویِ همین کد
# ساخته می‌شه (WEBHOOK_BASE + "/f/" + code) — نیازی به تلگرام برایِ باز
# کردنش نیست، هرکسی با هر مرورگری می‌تونه دانلود کنه.
class FileLink(Base):
    """یک فایلِ آپلودشده در ربات‌هایِ نوعِ «تبدیل فایل↔لینک»."""
    __tablename__ = "file_links"
    __table_args__ = (UniqueConstraint("code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    owner_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    code: Mapped[str] = mapped_column(String(24), index=True)
    orig_filename: Mapped[str] = mapped_column(String(255))
    disk_path: Mapped[str] = mapped_column(String(500))
    mime_type: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    download_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Config(Base):
    """یک کانفیگ در انبار."""
    __tablename__ = "configs"
    __table_args__ = (UniqueConstraint("bot_id", "code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    code: Mapped[int] = mapped_column(Integer, index=True)
    volume_gb: Mapped[int] = mapped_column(Integer)
    days: Mapped[int] = mapped_column(Integer)
    payload: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="in_stock")  # in_stock/sold
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("bot_users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # ── قیمت‌گذاری اختصاصی (مخصوص کانفیگ‌های نامحدودِ انبار) ──
    price: Mapped[int] = mapped_column(Integer, default=0)          # قیمت ثابت این کانفیگ (۰=قیمت سطح‌رباتی)
    price_per_day: Mapped[int] = mapped_column(Integer, default=0)  # قیمت هر روز (اختیاری، برای محاسبه/تمدید)
    user_count: Mapped[int] = mapped_column(Integer, default=0)     # تعداد کاربر مجاز (۰=نامشخص)
    display_mode: Mapped[str] = mapped_column(String(10), default="glass")  # glass | form
    # اسم دلخواه خریدار برای این کانفیگ (فقط برچسب نمایشی) — PATCH_MCFG_NAME_NOTE_PAY_V1
    custom_label: Mapped[str | None] = mapped_column(String(60))
    # دسته‌بندیِ دلخواهِ ادمین برای این کانفیگ (مثلاً «دسته ۱») — برای تفکیک انبار
    # و اتصال هر دسته به دکمهٔ فروشِ جداگانه در ربات — PATCH_MCFG_CATEGORY_V1
    category: Mapped[str | None] = mapped_column(String(20))
    # === PATCH_WIREGUARD_CONFIG_V1 ===
    # kind مشخص می‌کنه محتوای payload چیه:
    #   "sublink"    (پیش‌فرض، رفتارِ قبلی) → payload یک لینکِ ساب/سرور (متن)
    #   "wireguard"  → payload آیدیِ فایلِ تلگرامیِ یک فایلِ .conf وایرگارده
    #                  (نه خودِ محتوا — استفاده از فایلِ از‌قبل‌آپلودشده روی
    #                  سرورهای تلگرام، بدون نیاز به ذخیرهٔ باینری در دیتابیس)
    kind: Mapped[str] = mapped_column(String(12), default="sublink")


class FragmentWallet(Base):
    """کیف‌پولِ TONِ حضانتی که خودِ سیستم برایِ «استارزِ خودکار» (Fragment)
    می‌سازد — یک کیف‌پولِ جدا به‌ازایِ هر ربات، نه کیف‌پولِ شخصیِ مالک.

    === PATCH_FRAGMENT_WALLET_V1 ===
    برخلافِ TonDirectCharge (که فقط آدرسِ ولتِ *خودِ* فروشنده را رصد
    می‌کند)، اینجا کلیدِ خصوصی (mnemonic) توسطِ خودِ سیستم تولید و رمزنگاری‌شده
    (با wallet_crypto.py) ذخیره می‌شود — چون خریدِ خودکارِ استارز نیاز به
    امضایِ تراکنش دارد و مالکِ ربات نباید هر بار دستی امضا کند.
    عمداً blast-radius محدود است: فقط موجودیِ همین کیف‌پولِ اختصاصی در خطر
    است، نه کیف‌پولِ اصلیِ مالک."""
    __tablename__ = "fragment_wallets"
    __table_args__ = (UniqueConstraint("bot_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    address: Mapped[str] = mapped_column(String(80))
    wallet_version: Mapped[str] = mapped_column(String(10), default="v4r2")
    encrypted_mnemonic: Mapped[str] = mapped_column(Text)
    # کش — تا هر بار مجبور نباشیم زنده از toncenter بخوانیم
    last_balance_ton: Mapped[float] = mapped_column(Numeric(18, 9), default=0)
    last_balance_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # === PATCH_FRAGMENT_PURCHASE_V1 ===
    # فازِ ۲: اعتبارنامه‌هایِ لازم برایِ خریدِ واقعیِ استارز از طریقِ
    # pyfragment — کوکیِ سشنِ خودِ اکانتِ Fragment مالک (نه سرویسِ واسط) +
    # کلیدِ tonapi.io. هر دو مثلِ mnemonic رمزنگاری‌شده ذخیره می‌شن
    # (wallet_crypto.py)، چون دسترسیِ کامل به اکانتِ Fragment می‌دن.
    encrypted_fragment_cookies: Mapped[str | None] = mapped_column(Text)
    encrypted_tonapi_key: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class FragmentWalletArchive(Base):
    """=== PATCH_FRAGMENT_WALLET_ARCHIVE_V1 ===
    نسخهٔ بایگانیِ کیف‌پولِ TON حضانتیِ یک رباتِ حذف‌شده.

    چرا لازم شد: FragmentWallet کلیدِ خصوصیِ (mnemonic رمزنگاری‌شده) یک کیف‌پولِ واقعیِ TON
    را نگه می‌دارد که مالک ممکن است در آن موجودی واریز کرده باشد. حذفِ ربات این ردیف را
    (با FK به bots.id) پاک می‌کرد و کلید برای همیشه از دست می‌رفت. این جدول *بدونِ FK* است تا
    با حذفِ ربات پاک نشود؛ فقط قبل از حذف پر می‌شود و هیچ مسیرِ موجودی را تغییر نمی‌دهد.
    mnemonic با همان کلیدِ wallet_crypto.py رمزنگاری‌شده می‌ماند؛ کوکیِ Fragment و کلیدِ
    tonapi عمداً بایگانی نمی‌شوند (قابلِ ساخت مجدد‌اند و نگه‌داشتنشان لازم نیست)."""
    __tablename__ = "fragment_wallet_archive"

    id: Mapped[int] = mapped_column(primary_key=True)
    old_bot_id: Mapped[int] = mapped_column(Integer, index=True)
    owner_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    bot_username: Mapped[str | None] = mapped_column(String(64))
    address: Mapped[str] = mapped_column(String(80), index=True)
    wallet_version: Mapped[str] = mapped_column(String(10), default="v4r2")
    encrypted_mnemonic: Mapped[str] = mapped_column(Text)
    last_balance_ton: Mapped[float] = mapped_column(Numeric(18, 9), default=0)
    last_balance_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    wallet_created_at: Mapped[datetime | None] = mapped_column(DateTime)
    # bot_deleted
    reason: Mapped[str] = mapped_column(String(30), default="bot_deleted")
    archived_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SmmRefill(Base):
    """=== PATCH_SMM_REFILL_V1 ===
    یک درخواستِ ریفیل (جبرانی/گارانتی) رویِ پنلِ ارائه‌دهنده و پیگیریِ نتیجه‌اش.

    درخواستِ ادمین (فدایی): ریفیل فقط برایِ سرویسی که ارائه‌دهنده ریفیل دارد ثبت شود، و نتیجه
    (تکمیل/رد) به مشتری برسد. قبلاً فقط شناسهٔ ریفیل داخلِ یادداشتِ سفارش نوشته می‌شد و هیچ‌کس
    وضعیتش را نمی‌پرسید.

    status:
      requesting  — در حالِ تماس با پنل (جلوگیری از دوبار کلیک؛ بیش از ۲ دقیقه بماند کهنه حساب می‌شود)
      pending / in_progress — پنل پذیرفته، هنوز انجام نشده
      completed   — انجام شد (به مشتری اطلاع داده می‌شود)
      rejected    — پنل رد/لغو کرد (به مشتری اطلاع داده می‌شود)
      expired     — بیش از ۱۰ روز بی‌نتیجه؛ دیگر استعلام نمی‌شود
    """
    __tablename__ = "smm_refills"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    order_pk: Mapped[int] = mapped_column(ForeignKey("smm_orders.id"), index=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    panel_pk: Mapped[int | None] = mapped_column(ForeignKey("smm_panels.id"))
    panel_refill_id: Mapped[str | None] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(20), default="requesting", index=True)
    panel_status: Mapped[str | None] = mapped_column(String(60))
    notified_status: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TrialUsage(Base):
    """ثبت استفاده از تست رایگان (هر کاربر یک بار)."""
    __tablename__ = "trial_usages"
    __table_args__ = (UniqueConstraint("bot_id", "user_telegram_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ButtonTrialClaim(Base):
    """ثبتِ تعداد دفعاتِ دریافتِ «تست رایگان» هر کاربر از یک دکمهٔ مشخص
    (مستقل از جدولِ سراسریِ TrialUsage).

    فیلدِ gen امکانِ «ریست برای همه» را بدون پاک‌کردنِ رکوردها فراهم می‌کند:
    وقتی ادمین دکمهٔ ریست را می‌زند، مقدارِ trial_reset_gen در کانفیگِ همان
    دکمه (MainButton.config) یک واحد افزایش می‌یابد؛ در دفعهٔ بعدی که کاربر
    درخواست می‌دهد، چون gen ذخیره‌شده با gen جدید فرق دارد، شمارش از نو
    شروع می‌شود."""
    __tablename__ = "button_trial_claims"
    __table_args__ = (UniqueConstraint("mb_id", "user_telegram_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    mb_id: Mapped[int] = mapped_column(ForeignKey("main_buttons.id"), index=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger)
    gen: Mapped[int] = mapped_column(Integer, default=0)
    count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now())


class Order(Base):
    """یک سفارش خرید."""
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    volume_gb: Mapped[int] = mapped_column(Integer)
    days: Mapped[int] = mapped_column(Integer)
    count: Mapped[int] = mapped_column(Integer, default=1)
    amount: Mapped[float] = mapped_column(Numeric(12, 0))
    pay_method: Mapped[str] = mapped_column(String(20))  # wallet/card/vpn_card
    status: Mapped[str] = mapped_column(String(24), default="pending")
    # pending / awaiting_receipt / awaiting_admin / paid / rejected
    receipt_file_id: Mapped[str | None] = mapped_column(String(200))
    # آیدیِ یکتای فایلِ عکسِ رسید (Telegram file_unique_id) — برخلافِ file_id که
    # هربار می‌تواند فرق کند، این مقدار برای یک عکسِ مشخص همیشه ثابت است؛ برای
    # تشخیصِ رسیدِ تکراری حتی وقتی هوش مصنوعی نتواند شماره‌پیگیری/تاریخ را از
    # روی متنِ رسید بخواند (چون خودِ تشخیصِ عکس هیچ وابستگی‌ای به OCR ندارد).
    receipt_file_unique_id: Mapped[str | None] = mapped_column(String(200), index=True)
    receipt_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    discount_code: Mapped[str | None] = mapped_column(String(40))
    invoice_no: Mapped[str | None] = mapped_column(String(20), index=True)   # INV-BOTID-0001
    tracking_code: Mapped[str | None] = mapped_column(String(12), index=True) # کد پیگیری منحصربه‌فرد
    # کلید پنلی که این سفارش روی آن باید ساخته شود (فقط برای pay_method=vpn_card کاربرد دارد)
    panel_key: Mapped[str | None] = mapped_column(String(30))
    # === PATCH_ORDER_PLAN_ID_V1 ===
    # پلنی که کاربر دقیقاً رویش کلیک کرده. قبلاً فقط حجم/مدت ذخیره می‌شد و
    # اگر ربات دو بستهٔ هم‌حجم با قیمتِ متفاوت داشت («اقتصادی» و «حرفه‌ای»)،
    # ادمین از رویِ رسید نمی‌فهمید خرید از کدام دکمه بوده. FK عمداً نیست تا
    # حذفِ پلن، سفارش‌هایِ تاریخی را از بین نبرد.
    plan_id: Mapped[int | None] = mapped_column(Integer, index=True)
    # === PATCH_ORDER_BUTTON_ID_V1 ===
    # دکمه‌ی صفحه‌ی اصلی که خرید از طریقش شروع شده (مثلاً «🇩🇪 آلمان»). برایِ
    # وقتی که چند دکمه‌ی متفاوت (با اسم‌هایِ منطقه‌ای/بازاریابی) به یه پنلِ
    # واحد وصلن و ادمین می‌خواد تو گزارشِ کانالِ اعتماد ببینه دقیقاً کدوم
    # دکمه بوده، نه فقط اسمِ پنل. FK عمداً نیست تا حذفِ دکمه، سفارش‌هایِ
    # تاریخی رو از بین نبره.
    button_id: Mapped[int | None] = mapped_column(Integer, index=True)
    # نام دلخواهِ کاربر برای کانفیگ (فقط برچسب نمایشی؛ روی پنل اثری ندارد) — اختیاری
    custom_label: Mapped[str | None] = mapped_column(String(60))
    # کانفیگِ رزروشده از انبار دستی برای این سفارش (فقط pay_method=mcfg_card) — PATCH_MCFG_NAME_NOTE_PAY_V1
    reserved_config_id: Mapped[int | None] = mapped_column(ForeignKey("configs.id"))
    # ۴ رقمِ آخرِ کارتِ مبدأ/ارسال‌کننده که کاربر هنگامِ پرداختِ کارت‌به‌کارت
    # اعلام کرده (فقط وقتی تشخیصِ خودکارِ رسید فعال باشد پرسیده می‌شود) — PATCH_SENDER_CARD_CHECK
    sender_card4: Mapped[str | None] = mapped_column(String(4))
    # PATCH_ADD_SHEBA_ACCOUNT_ID_TYPE — نوعِ شناسه‌ای که کاربر اعلام کرده: card/sheba/account
    sender_id_type: Mapped[str | None] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# === PATCH_PRODUCT_RATING_V1 ===
class ProductRating(Base):
    """امتیازِ ستاره‌ایِ کاربر به یک خریدِ انجام‌شده.

    برایِ هر سفارش فقط یک امتیاز ثبت می‌شود (قیدِ یکتا). ستونِ asked_at
    نشون می‌ده درخواستِ امتیاز کِی برایِ این سفارش فرستاده شده (تا دوباره
    فرستاده نشه)؛ stars تا وقتی کاربر امتیاز نداده NULL می‌مونه.
    """
    __tablename__ = "product_ratings"
    __table_args__ = (UniqueConstraint("bot_id", "order_kind", "order_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    # نوعِ سفارش: cfg (کانفیگ/VPN) | shop (فروشگاه) | smm (خدماتِ مجازی)
    # چون هر کدوم جدولِ سفارشِ جدا دارن، آیدیِ سفارش به‌تنهایی یکتا نیست.
    order_kind: Mapped[str] = mapped_column(String(10), default="cfg")
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    # شناسهٔ یکتایِ خودِ محصول برایِ محاسبهٔ میانگین — مثلاً "cfg:12" یا
    # "shop:5". رشته‌ست (نه FK) تا با انواعِ مختلفِ محصول کار کنه.
    item_key: Mapped[str | None] = mapped_column(String(40), index=True)
    stars: Mapped[int | None] = mapped_column(Integer)
    asked_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    rated_at: Mapped[datetime | None] = mapped_column(DateTime)


class ButtonSetting(Base):
    """تنظیمات دکمه‌های قابل‌ویرایش."""
    __tablename__ = "button_settings"
    __table_args__ = (UniqueConstraint("bot_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    key: Mapped[str] = mapped_column(String(40))
    label: Mapped[str] = mapped_column(String(80))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    color: Mapped[str | None] = mapped_column(String(20))


class Setting(Base):
    """تنظیمات عمومی هر ربات."""
    __tablename__ = "settings"
    __table_args__ = (UniqueConstraint("bot_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    key: Mapped[str] = mapped_column(String(60))
    value: Mapped[str] = mapped_column(Text)


class DiscountCode(Base):
    """کدهای تخفیف هر ربات."""
    __tablename__ = "discount_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    code: Mapped[str] = mapped_column(String(40))
    percent: Mapped[int] = mapped_column(Integer, default=0)    # درصد تخفیف (وقتی disc_type=percent)
    max_uses: Mapped[int] = mapped_column(Integer, default=0)   # 0=نامحدود
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # ── قابلیت‌های توسعه‌یافته (نسخهٔ کامل کد تخفیف) ──
    # نوع تخفیف: "percent" = درصدی | "amount" = مبلغ ثابت (تومان)
    disc_type: Mapped[str] = mapped_column(String(10), default="percent")
    # مبلغ ثابت تخفیف به تومان (وقتی disc_type=amount استفاده می‌شود)
    amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    # تاریخ انقضای کد (UTC، naive)؛ None = بدون انقضا
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    # محدود به یک پنل خاص (sanai/...)؛ None = همهٔ پنل‌ها
    panel_key: Mapped[str | None] = mapped_column(String(30))
    # محدود به یک پلن خاص (VpnPlan.id)؛ None = همهٔ پلن‌ها
    plan_id: Mapped[int | None] = mapped_column(Integer)
    # حداقل مبلغ خرید برای فعال‌شدن کد (تومان)؛ 0 = بدون حداقل
    min_amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    # محدود به یک دکمهٔ خاص (main_buttons.id)؛ None = عمومی (مثل قبل).
    # وقتی مقدار دارد، این کد فقط در جریانِ خریدِ همان دکمه معتبر است (کد تخفیف اختصاصی دکمه).
    button_id: Mapped[int | None] = mapped_column(Integer, index=True)
    # محدود به یک کاربر خاص (telegram_id)؛ None = برای همه معتبر.
    # وقتی مقدار دارد، این کد فقط با همین آیدی عددی تلگرام قابل‌استفاده است.
    restricted_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    # === PATCH_VS_DISCOUNT_PRODUCTS_V1 ===
    # محدود به دسته‌هایِ محصولِ «خدمات مجازی» (کلیدها با کاما: stars,ton,gift,premium,vnum,smm).
    # None/خالی = همهٔ محصولات (مثلِ قبل). اگر پر باشد، کد فقط در همان دسته‌ها معتبر است
    # و در جریان‌هایی که دسته‌شان را نمی‌دانند (فروشگاه/VPN) رد می‌شود.
    product_keys: Mapped[str | None] = mapped_column(String(120))


class DiscountUsage(Base):
    """ردیابی استفاده هر کاربر از کد تخفیف (هر نفر یک بار)."""
    __tablename__ = "discount_usages"

    id: Mapped[int] = mapped_column(primary_key=True)
    discount_code_id: Mapped[int] = mapped_column(ForeignKey("discount_codes.id"), index=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    # مبلغِ صرفه‌جویی‌شده (تومان) در این استفادهٔ خاص — برای گزارشِ «کل مبلغ
    # تخفیفِ استفاده‌شده». رکوردهای قدیمی‌تر (قبل از این فیلد) مقدارِ ۰ دارند.
    saved_amount: Mapped[float] = mapped_column(Numeric(12, 0), default=0)
    used_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TenantTicket(Base):
    """تیکت پشتیبانی کاربران ربات مستأجر."""
    __tablename__ = "tenant_tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    photo_file_id: Mapped[str | None] = mapped_column(String(300))  # اسکرین‌شاتِ ضمیمه (اختیاری)
    status: Mapped[str] = mapped_column(String(12), default="open")  # open/answered/closed
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # === PATCH_TICKET_CATEGORY_SUBJECT_V1 ===
    # هر دو nullable هستن تا تیکت‌هایِ قدیمی (قبل از این پچ) بدونِ مهاجرتِ
    # دستی سالم بمونن؛ کدِ نمایش هرجا خالی بود، اون بخش رو حذف می‌کنه نه
    # اینکه «—»یِ بی‌معنی نشون بده.
    category: Mapped[str | None] = mapped_column(String(24))  # billing/technical/order/other
    subject: Mapped[str | None] = mapped_column(String(100))
    # === PATCH_TICKET_RATING_V1 ===
    rating: Mapped[int | None] = mapped_column(Integer)  # ۱ تا ۵ — فقط بعدِ بسته‌شدنِ تیکت قابل‌ثبت


class TenantTicketReply(Base):
    """پاسخ‌های تیکت."""
    __tablename__ = "tenant_ticket_replies"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tenant_tickets.id"), index=True)
    sender: Mapped[str] = mapped_column(String(8))  # user/admin
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Broadcast(Base):
    """ارسال همگانی."""
    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/sending/done/cancelled/interrupted
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # === PATCH_BROADCAST_DURABLE_V1 ===
    # ارسالِ همگانی قبلاً یک تسکِ حافظه‌ایِ داخلِ پروسهٔ وب بود؛ هر ری‌استارت (مثلاً اعمالِ زیپ) بی‌صدا
    # قطعش می‌کرد و ردیف برایِ همیشه «sending» می‌ماند. حالا کلِ کار این‌جا ذخیره می‌شود و ورکر آن را
    # از همان نفری که مانده بود ادامه می‌دهد.
    media_id: Mapped[str | None] = mapped_column(String(300))
    media_type: Mapped[str | None] = mapped_column(String(16))
    kb_json: Mapped[str | None] = mapped_column(Text)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)      # چتِ مدیر برایِ پیامِ پیشرفت
    msg_id: Mapped[int | None] = mapped_column(BigInteger)       # پیامِ «در حالِ ارسال…»
    cursor_uid: Mapped[int | None] = mapped_column(Integer)      # آخرین BotUser.id که پردازش شد
    total_count: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)  # ضربانِ اجرا (برایِ تشخیصِ اجرایِ مرده)
    # === PATCH_BROADCAST_AUDIENCE_V1 === (تیکت #863) مخاطب: خالی=همه | svc_any | svc_unlimited | svc_none | panel:<key>
    audience: Mapped[str | None] = mapped_column(String(60), nullable=True)


class ForceJoin(Base):
    """عضویت اجباری در کانال/گروه."""
    __tablename__ = "force_joins"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    chat_id: Mapped[str] = mapped_column(String(80))    # آیدی یا یوزرنیم کانال
    title: Mapped[str | None] = mapped_column(String(120))
    invite_link: Mapped[str | None] = mapped_column(String(256))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # === PATCH_FORCEJOIN_BTN_COLOR_V1 ===
    # رنگِ دکمهٔ همین کانال در پیامِ عضویتِ اجباری: danger/success/primary
    # یا خالی (بی‌رنگ). فقط پلنِ حرفه‌ای می‌تواند تنظیمش کند.
    btn_color: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AdminSupport(Base):
    """ادمین‌های پشتیبانی ربات مستأجر."""
    __tablename__ = "admin_supports"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger)
    full_name: Mapped[str | None] = mapped_column(String(120))
    added_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # === PATCH_ADMIN_GRANULAR_PERMS_V1 ===
    # آرایهٔ JSON از کلیدهایِ بخش‌هایِ مجاز (مثلاً ["products","stats"]).
    # رشتهٔ خالی = دسترسیِ کامل (سازگار با ادمین‌هایِ قدیمی که این ستون
    # را نداشتند).
    permissions: Mapped[str] = mapped_column(Text, default="")


# ===========================================================================
#  مدل‌های ربات مادر (بات‌ساز)
# ===========================================================================

class PlatformUser(Base):
    """کاربر خودِ ربات بات‌ساز."""
    __tablename__ = "platform_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(120))
    # === PATCH_PLATFORM_USER_USERNAME_V1 ===
    # قبلاً یوزرنیم اصلاً ذخیره نمی‌شد؛ جستجوی «با یوزرنیم» فقط به
    # bot.get_chat("@username") تکیه داشت که تلگرام روزبه‌روز محدودترش
    # می‌کنه (فقط برای کاربرهایی که اخیراً با بات تعامل داشتن/یوزرنیمِ
    # عمومی‌شون قابل‌دیدنه کار می‌کنه). الان یوزرنیم همین‌جا هم ذخیره
    # می‌شه تا جستجو مستقل از این محدودیت باشه.
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    balance: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    referrer_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    spam_warnings: Mapped[int] = mapped_column(Integer, default=0)  # تعداد اخطارهای آنتی‌اسپم (۳ام = بن)
    extra_bot_slots: Mapped[int] = mapped_column(Integer, default=0)  # اسلات‌های اضافه‌ی خریداری‌شده با استارز
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # === PATCH_MEMBER_STATS_V1 ===
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime)  # آخرین تعاملِ کاربر با ربات
    channel_status: Mapped[str | None] = mapped_column(String(16))  # "joined" | "not_joined" | None=هنوز چک‌نشده
    channel_checked_at: Mapped[datetime | None] = mapped_column(DateTime)  # آخرین باری که وضعیتِ عضویت چک شد
    ever_joined: Mapped[bool] = mapped_column(Boolean, default=False)  # حداقل یک‌بار عضوِ کانال بوده (برایِ تشخیصِ لفت‌دادن)
    # === PATCH_PLATFORM_ONBOARDING_V1 ===
    # معادلِ BotUser.rules_accepted در تنانت — برایِ «تجربهٔ ورودِ حرفه‌ای»ِ
    # خودِ ربات اصلیِ بات‌ساز (platform_bot.py)، نه ربات‌های مستأجر.
    rules_accepted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    rules_accepted_at: Mapped[datetime | None] = mapped_column(DateTime)
    # === PATCH_PLATFORM_PROMOTER_V1 === معرفِ فروش (لینک/کدِ تخفیفِ خریدِ اول)
    promoter_id: Mapped[int | None] = mapped_column(Integer, index=True)


class InviteCampaignEntry(Base):
    """شرکت‌کنندهٔ طرح «جایزهٔ ویژهٔ دعوت از دوستان» ربات مادر (فقط یک‌بار برای هر کاربر)."""
    __tablename__ = "invite_campaign_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending / won
    win_day: Mapped[str | None] = mapped_column(String(10))       # YYYY-MM-DD (تقویم ایران) — روزی که برنده شد
    won_at: Mapped[datetime | None] = mapped_column(DateTime)     # زمانِ دقیقِ برنده‌شدن
    trial_req_id: Mapped[int | None] = mapped_column(Integer)     # شناسه‌ی درخواستِ ترایالِ رایگانِ جایزه


class ChannelRaffle(Base):
    """یک دورِ «نظرسنجی/قرعه‌کشی» که ادمین توی کانال/گروهِ بات‌ساز راه‌انداخته.
    شروعِ بازه‌ی رأی‌گیری = زمانِ ارسالِ خودِ پیامِ نظرسنجی (poll_started_at)."""
    __tablename__ = "channel_raffles"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    poll_message_id: Mapped[int] = mapped_column(BigInteger)
    telegram_poll_id: Mapped[str | None] = mapped_column(String(64), index=True)  # فقط وقتی خودِ بات فرستاده
    # "poll": مثلِ قبل، بر اساسِ ریپلای‌کردن با شماره‌ی گزینه.
    # "mention": شرکت‌کننده کسیه که زیرِ پیام یه نفرِ دیگه رو منشن کرده —
    # مفهومِ «جوابِ درست» نداره، همه‌ی منشن‌کننده‌ها واجدِ شرایطِ قرعه‌کشی‌ان.
    mode: Mapped[str] = mapped_column(String(10), default="poll")
    topic: Mapped[str] = mapped_column(Text)                    # متنِ کاملِ پیامِ نظرسنجی
    options_json: Mapped[str] = mapped_column(Text)              # JSON: ["رئال", "بارسلونا"]
    poll_started_at: Mapped[datetime] = mapped_column(DateTime)  # زمانِ خودِ پیامِ نظرسنجی
    duration_hours: Mapped[float] = mapped_column(Float)
    correct_option: Mapped[int | None] = mapped_column(Integer)  # ۱-پایه؛ تا وقتی معلوم نشده None
    # open: هنوز بازهٔ رأی‌گیری تمام نشده یا جوابِ درست معلوم نشده
    # closed: جوابِ درست معلوم شده، آمار حاضر است ولی قرعه‌کشی هنوز انجام نشده
    # drawn: قرعه‌کشی انجام و برندگان ثبت شده‌اند
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ChannelRaffleVote(Base):
    """یک رأی (پاسخِ عددی به‌صورتِ ریپلای زیرِ پیامِ نظرسنجی)."""
    __tablename__ = "channel_raffle_votes"
    __table_args__ = (UniqueConstraint("raffle_id", "telegram_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    raffle_id: Mapped[int] = mapped_column(ForeignKey("channel_raffles.id"), index=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    full_name: Mapped[str | None] = mapped_column(String(120))
    username: Mapped[str | None] = mapped_column(String(64))
    chosen_option: Mapped[int] = mapped_column(Integer)  # ۱-پایه
    voted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ChannelRaffleWinner(Base):
    """یکی از ۵ برندهٔ یک دورِ قرعه‌کشی."""
    __tablename__ = "channel_raffle_winners"

    id: Mapped[int] = mapped_column(primary_key=True)
    raffle_id: Mapped[int] = mapped_column(ForeignKey("channel_raffles.id"), index=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    full_name: Mapped[str | None] = mapped_column(String(120))
    username: Mapped[str | None] = mapped_column(String(64))
    rank: Mapped[int] = mapped_column(Integer)  # ۱ تا ۵
    prize_amount: Mapped[float] = mapped_column(Numeric(12, 0), default=0)
    prize_given_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending/won
    trial_req_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # id در plan_trial_requests
    win_day: Mapped[str | None] = mapped_column(String(10), nullable=True)    # YYYY-MM-DD (تقویم ایران)
    joined_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    won_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RaffleGroupMember(Base):
    """اخطارها و وضعیتِ بنِ کاربران توی گروهِ قرعه‌کشیِ بات‌ساز — برای
    سیستمِ ضدِ تبلیغ (لینک/منشن) در گروه. جدا از PlatformUser چون این
    مربوطِ به گروه‌ست، نه پلتفرمِ اصلی، و افراد ممکنه هرگز با بات چت
    نکرده باشن ولی توی گروه پیام بذارن."""
    __tablename__ = "raffle_group_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(120))
    username: Mapped[str | None] = mapped_column(String(64))
    warnings: Mapped[int] = mapped_column(Integer, default=0)      # تعداد اخطارهای ضدِ تبلیغ (سومی = بن)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)  # آیا از گروه بن شده
    last_warning_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_warning_reason: Mapped[str | None] = mapped_column(String(255))
    banned_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AdBroadcast(Base):
    """پیکربندیِ اصلی سیستمِ ارسالِ زمان‌بندی‌شدهٔ تبلیغ برای بات‌های ad_bot.
    هر باتِ ad_bot دقیقاً یک ردیف تنظیمات داره."""
    __tablename__ = "ad_broadcasts"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), unique=True, index=True)
    # متنِ تبلیغ به‌صورت HTML — با تمامِ فرمت‌های تلگرام (bold, italic,
    # underline, strikethrough, spoiler, code, pre, tg-emoji, text_link, …)
    ad_text_html: Mapped[str | None] = mapped_column(Text)
    ad_photo_file_id: Mapped[str | None] = mapped_column(String(255))  # اختیاری — عکسِ همراه تبلیغ
    ad_video_file_id: Mapped[str | None] = mapped_column(String(255))  # اختیاری — ویدیوی همراه
    # === PATCH_AD_GIFT_LINK_V1 ===
    # لینک گیفت تلگرام (t.me/nft/...) — اگه پر باشه، تبلیغ به‌صورت متن با
    # پیش‌نمایشِ بزرگِ همین لینک ارسال می‌شه تا گیفت پرمیوم زیرش دیده بشه.
    ad_gift_url: Mapped[str | None] = mapped_column(String(255))
    interval_minutes: Mapped[int] = mapped_column(Integer, default=1440)  # پیش‌فرض: روزی یک بار
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_send_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    total_sent: Mapped[int] = mapped_column(Integer, default=0)  # تعدادِ کلِ ارسال‌های موفق
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # === PATCH_AD_PRODUCT_BUTTON_V1 ===
    # اگه پر باشه، زیرِ هر پستِ تبلیغ یه دکمهٔ آبیِ اینلاین اضافه می‌شه که
    # مستقیم کاربر رو به صفحهٔ همین محصول (داخلِ خودِ بات) می‌بره —
    # با دیپ‌لینکِ t.me/<bot>?start=prod_<id>.
    linked_product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id"), nullable=True)


class AdBroadcastTarget(Base):
    """کانال یا گروهی که تبلیغ اونجا ارسال می‌شه."""
    __tablename__ = "ad_broadcast_targets"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)  # -100... برای کانال/سوپرگروه
    title: Mapped[str | None] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(16))  # channel | group | supergroup
    added_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(String(255))  # آخرین خطا (اگه ارسال نشد)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)


# === PATCH_AD_ONETIME_SCHEDULE_V1 ===
class AdScheduledPost(Base):
    """یک پستِ تبلیغاتیِ «یک‌بارهٔ زمان‌بندی‌شده» — دقیقاً در یک تاریخ/ساعتِ
    مشخص، یک‌بار به همهٔ کانال/گروه‌های وصل‌شدهٔ همون بات (AdBroadcastTarget)
    فرستاده می‌شه؛ کاملاً مستقل از AdBroadcastِ تکرارشونده — هر بات می‌تونه
    هم‌زمان هم یه تبلیغِ چرخشیِ دائمی داشته باشه، هم چندین پستِ یک‌بارهٔ
    زمان‌بندی‌شده برای تاریخ‌های مختلف."""
    __tablename__ = "ad_scheduled_posts"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    ad_text_html: Mapped[str | None] = mapped_column(Text)
    ad_photo_file_id: Mapped[str | None] = mapped_column(String(255))
    ad_video_file_id: Mapped[str | None] = mapped_column(String(255))
    ad_gift_url: Mapped[str | None] = mapped_column(String(255))  # PATCH_AD_GIFT_LINK_V1
    linked_product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    # pending: هنوز نرسیده / در انتظار | sent: با موفقیت ارسال شد | failed: زمانش گذشت ولی هیچ هدفی نداشت
    status: Mapped[str] = mapped_column(String(12), default="pending")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)  # به چندتا هدف موفق فرستاده شد
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class InviteCampaignWinner(Base):
    """فهرست برندگانِ روزانهٔ طرح دعوت (برای نمایش در صفحه و کنترل سقفِ ۵ نفرِ روزانه)."""
    __tablename__ = "invite_campaign_winners"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    full_name: Mapped[str | None] = mapped_column(String(120))
    day: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD (تقویم ایران)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class WalletCharge(Base):
    """درخواست شارژ کیف پول."""
    __tablename__ = "wallet_charges"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 0))
    receipt_file_id: Mapped[str | None] = mapped_column(String(200))
    receipt_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ۴ رقمِ آخرِ کارتِ مبدأ که کاربر هنگامِ پرداخت اعلام می‌کند (برای تطبیقِ خودکار رسید)
    sender_card4: Mapped[str | None] = mapped_column(String(4))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/approved/rejected
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class UsedReceipt(Base):
    """اثرِ انگشتِ رسیدهای تأییدشده، برای جلوگیری از ارسالِ مجددِ یک رسید.

    fingerprint معمولاً شمارهٔ پیگیریِ رسید است؛ اگر نبود از ترکیبِ
    تاریخ+ساعت+مبلغ ساخته می‌شود. scope مشخص می‌کند این رسید برای پلتفرم
    استفاده شده یا برای کدام رباتِ مستأجر (bot_id)."""
    __tablename__ = "used_receipts"

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(32), index=True)  # "platform" یا "tenant:<bot_id>"
    fingerprint: Mapped[str] = mapped_column(String(120), index=True)
    amount: Mapped[float | None] = mapped_column(Numeric(14, 0))
    user_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("scope", "fingerprint", name="uq_used_receipt_scope_fp"),
    )


class PlanOrder(Base):
    """خرید پلن."""
    __tablename__ = "plan_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_bot_id: Mapped[int | None] = mapped_column(ForeignKey("bots.id"))
    plan: Mapped[str] = mapped_column(String(20))
    amount: Mapped[float] = mapped_column(Numeric(14, 0))
    status: Mapped[str] = mapped_column(String(16), default="paid")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PlatformTxn(Base):
    """دفتر تراکنش‌های کیف پول."""
    __tablename__ = "platform_txns"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 0))
    kind: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PlatformRating(Base):
    """امتیاز کاربران به خودِ بات‌ساز (هر کاربر یک امتیاز؛ قابل به‌روزرسانی)."""
    __tablename__ = "platform_ratings"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    stars: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Ticket(Base):
    """تیکت پشتیبانی کاربران بات‌ساز."""
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    text: Mapped[str] = mapped_column(Text)
    photo_file_id: Mapped[str | None] = mapped_column(String(300))  # اسکرین‌شاتِ ضمیمه (اختیاری)
    status: Mapped[str] = mapped_column(String(12), default="open")
    admin_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TicketReply(Base):
    """پاسخ‌های تیکت."""
    # PATCH_TICKET_ADMIN_PHOTO_MARKER
    __tablename__ = "ticket_replies"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    sender: Mapped[str] = mapped_column(String(8))  # user/admin
    text: Mapped[str] = mapped_column(Text)
    photo_file_id: Mapped[str | None] = mapped_column(String(300))  # عکس ضمیمهٔ پاسخ (اختیاری)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SubAdmin(Base):
    """ادمین فرعی بات‌ساز با دسترسی‌های محدود."""
    __tablename__ = "sub_admins"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(120))
    permissions: Mapped[str] = mapped_column(Text, default="")  # JSON آرایه‌ای از کلیدهای دسترسی
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PlanFeatureOverride(Base):
    """تنظیمات بخش‌های قابل دسترس برای هر پلن — توسط مدیر اصلی قابل تغییر."""
    __tablename__ = "plan_feature_overrides"
    __table_args__ = (UniqueConstraint("plan", "feature_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    plan: Mapped[str] = mapped_column(String(20))       # free/growth/pro
    feature_key: Mapped[str] = mapped_column(String(60))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class MainButton(Base):
    """دکمهٔ صفحهٔ اصلیِ ربات مستأجر."""
    __tablename__ = "main_buttons"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    row: Mapped[int] = mapped_column(Integer)
    col: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(80))
    action: Mapped[str | None] = mapped_column(String(30))
    message: Mapped[str | None] = mapped_column(Text)
    rules: Mapped[str | None] = mapped_column(Text)
    config: Mapped[str | None] = mapped_column(Text)
    color: Mapped[str | None] = mapped_column(String(20))
    icon_emoji_id: Mapped[str | None] = mapped_column(String(40))  # شناسهٔ ایموجی پرمیوم کنار متن دکمه
    support_btn: Mapped[bool] = mapped_column(Boolean, default=False)  # نمایش دکمهٔ «پیام به پشتیبانی» زیر این دکمه
    # آدرس پشتیبانِ اختصاصیِ همین دکمه (آیدی عددی/یوزرنیم/لینک). اگر خالی باشد،
    # هنگام نمایش به‌صورت fallback از آدرس عمومیِ ربات (در صورت وجود) استفاده می‌شود.
    support_contact: Mapped[str | None] = mapped_column(String(150))
    parent_id: Mapped[int | None] = mapped_column(Integer, index=True)  # زیرمنو: شناسهٔ دکمهٔ والد
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ===========================================================================
#  مدل‌های جدید — پشتیبانی از چند زمینه (multi-field)
# ===========================================================================

class Product(Base):
    """محصول در فروشگاه دیجیتال / دوره آموزشی."""
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[float] = mapped_column(Numeric(12, 0), default=0)
    category: Mapped[str | None] = mapped_column(String(60))
    file_id: Mapped[str | None] = mapped_column(String(200))       # فایل تلگرام
    photo_id: Mapped[str | None] = mapped_column(String(200))      # عکس محصول
    stock: Mapped[int] = mapped_column(Integer, default=-1)        # -1 = نامحدود
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    shop_link_url: Mapped[str | None] = mapped_column(String(300))   # لینک فروشگاه/سایت بیرونی
    shop_link_mode: Mapped[str | None] = mapped_column(String(10))   # "webapp" | "browser"
    # === PATCH_PRODUCT_PIN_ON_START_V1 ===
    # اگه روشن باشه، این محصول بلافاصله بعد از پیامِ خوش‌آمدِ /start برای
    # کاربر ارسال می‌شه (قابلِ روشن/خاموش از پنلِ مدیریتِ محصول).
    pin_on_start: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # === PATCH_PRODUCT_GIFT_LINK_V1 ===
    # لینک گیفت پرمیوم (t.me/nft/...) — اگه پر باشه، صفحهٔ محصول با
    # پیش‌نمایشِ بزرگِ همین لینک نشون داده می‌شه (تصویرِ متحرکِ گیفت).
    gift_link_url: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # ── گیفت کلکسیونی/استارزیِ تلگرام ──
    is_gift: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    gift_id: Mapped[str | None] = mapped_column(String(64))          # شناسه گیفت تلگرام (getAvailableGifts)
    gift_star_count: Mapped[int | None] = mapped_column(Integer)     # هزینه استارزیِ گیفت نزد تلگرام


class ProductOrder(Base):
    """خرید محصول."""
    __tablename__ = "product_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    amount: Mapped[float] = mapped_column(Numeric(12, 0))
    pay_method: Mapped[str] = mapped_column(String(20))     # wallet/card
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # pending / paid / delivered / rejected
    receipt_file_id: Mapped[str | None] = mapped_column(String(200))
    receipt_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    tracking_code: Mapped[str | None] = mapped_column(String(12), index=True)
    discount_code_id: Mapped[int | None] = mapped_column(ForeignKey("discount_codes.id"))
    # === PATCH_GIFT_TO_FRIEND_V1 ===
    # اگه پر باشه، گیفتِ این سفارش به‌جای خودِ خریدار برای این آیدی ارسال می‌شه.
    gift_recipient_tg_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Appointment(Base):
    """نوبت رزرو شده."""
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    service_name: Mapped[str] = mapped_column(String(120))
    date_str: Mapped[str] = mapped_column(String(20))           # YYYY-MM-DD
    time_str: Mapped[str] = mapped_column(String(10))           # HH:MM
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    # pending / confirmed / cancelled / done
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AppointmentSlot(Base):
    """اسلات‌های زمانی قابل رزرو."""
    __tablename__ = "appointment_slots"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)       # 0=شنبه .. 6=جمعه
    time_str: Mapped[str] = mapped_column(String(10))   # HH:MM
    capacity: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Poll(Base):
    """نظرسنجی / رای‌گیری."""
    __tablename__ = "polls"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    question: Mapped[str] = mapped_column(Text)
    options_json: Mapped[str] = mapped_column(Text)     # JSON آرایه گزینه‌ها
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_multiple: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PollVote(Base):
    """رای کاربر در نظرسنجی.

    محدودیت یکتایی روی (poll_id, user_id) است نه روی option_index؛ چون قانون
    کسب‌وکار «یک رای به ازای هر کاربر در هر نظرسنجی» است (مگر allow_multiple=True
    باشد). محدودیت قبلی روی (poll_id, user_id, option_index) فقط رای دوباره به
    همان گزینه را می‌گرفت و کاربر می‌توانست با زدن گزینه‌های مختلف چند رای ثبت کند.
    """
    __tablename__ = "poll_votes"
    __table_args__ = (UniqueConstraint("poll_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    poll_id: Mapped[int] = mapped_column(ForeignKey("polls.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    option_index: Mapped[int] = mapped_column(Integer)
    voted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ReferralLink(Base):
    """لینک دعوت با ترکینگ."""
    __tablename__ = "referral_links"
    __table_args__ = (UniqueConstraint("bot_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    click_count: Mapped[int] = mapped_column(Integer, default=0)
    join_count: Mapped[int] = mapped_column(Integer, default=0)
    reward_per_join: Mapped[float] = mapped_column(Numeric(12, 0), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ReferralJoin(Base):
    """ردیابی دعوت‌های شمارش‌شده — هر کاربر فقط یک‌بار به‌عنوان «دعوت‌شده» حساب می‌شود.

    وجود این رکورد یعنی این telegram_id قبلاً برای این ربات به‌عنوان دعوت شمارش
    شده است؛ از همین رو UniqueConstraint روی (bot_id, referred_telegram_id) از
    پاداشِ تکراری (کلیک چندبارهٔ همان شخص روی لینک) جلوگیری می‌کند.
    """
    __tablename__ = "referral_joins"
    __table_args__ = (UniqueConstraint("bot_id", "referred_telegram_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    referral_link_id: Mapped[int] = mapped_column(ForeignKey("referral_links.id"), index=True)
    inviter_user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    referred_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    reward_paid: Mapped[float] = mapped_column(Numeric(12, 0), default=0)
    purchase_rewarded: Mapped[bool] = mapped_column(Boolean, default=False)
    # === PATCH_REFPOINT_REQUIRE_ORDER_V1 === امتیازِ نگه‌داشته‌شده تا دعوت‌کننده اولین سفارشش را ثبت کند
    points_pending: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ReferralOrderReward(Base):
    """=== PATCH_REF_VS_RECONCILE_V1 === سفارشِ خدماتِ مجازی که پاداشِ دعوتش بررسی شده (یکتا → بدونِ پاداشِ تکراری)."""
    __tablename__ = "referral_order_rewards"
    __table_args__ = (UniqueConstraint("bot_id", "kind", "order_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10))
    order_id: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MeteredAccount(Base):
    """=== PATCH_METERED_PLAN_V1 === سرویسِ «بستهٔ اعتباری»: مصرف از کیف پول کم می‌شود.

    account_id = PanelAccount.id (عمداً بدونِ FK: حذفِ سرویس توسطِ کاربر/ادمین نباید گیر کند؛ حلقه خودش «ended» می‌کند).
    last_used_bytes = مصرفی که تا حالا پولش کسر شده؛ billed_toman = جمعِ کسرها.
    status: active | no_credit (موجودی تمام، سرویس قطع) | ended (منقضی/حذف)."""
    __tablename__ = "metered_accounts"
    __table_args__ = (UniqueConstraint("account_id", name="uq_metered_account"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    account_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    price_per_gb: Mapped[int] = mapped_column(Integer, default=0)
    # سقفِ مجموعِ حجمِ این بسته (گیگ)؛ ۰ = فقط به‌اندازهٔ کیف پول — PATCH_METERED_MULTI_PLAN_V1
    max_gb: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_used_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    billed_toman: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class StockDelivery(Base):
    """=== PATCH_STOCK_MANUAL_DELIVERY_V1 === سفارشِ «کانفیگ آماده» که پرداخت شده ولی ادمین باید دستی تحویل بدهد.

    status: pending | delivered | refunded. پولِ سفارش همان لحظه ثبت شده (Order.status=paid)؛ این جدول فقط صفِ تحویل است."""
    __tablename__ = "stock_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    order_id: Mapped[int | None] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    volume_gb: Mapped[int] = mapped_column(Integer, default=0)
    days: Mapped[int] = mapped_column(Integer, default=0)
    count: Mapped[int] = mapped_column(Integer, default=1)
    amount: Mapped[int] = mapped_column(Integer, default=0)
    label: Mapped[str | None] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    delivered_by: Mapped[int | None] = mapped_column(BigInteger)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    done_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BotReview(Base):
    """نظر/امتیاز کاربر برای ربات."""
    __tablename__ = "bot_reviews"
    __table_args__ = (UniqueConstraint("bot_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    rating: Mapped[int] = mapped_column(Integer)        # 1-5
    comment: Mapped[str | None] = mapped_column(Text)
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AutoMessage(Base):
    """پیام خودکار (خوش‌آمد، یادآوری و ...)."""
    __tablename__ = "auto_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    trigger: Mapped[str] = mapped_column(String(40))
    # welcome / first_purchase / birthday / inactive_7d / inactive_30d
    text: Mapped[str] = mapped_column(Text)
    photo_id: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class BotMenuItem(Base):
    """آیتم منوی سفارشی برای ربات‌های غیر VPN."""
    __tablename__ = "bot_menu_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("bot_menu_items.id"))
    label: Mapped[str] = mapped_column(String(80))
    content: Mapped[str | None] = mapped_column(Text)      # متن یا دستور
    action: Mapped[str | None] = mapped_column(String(30))  # show_text/show_products/book/poll
    photo_id: Mapped[str | None] = mapped_column(String(200))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class VpnPanel(Base):
    """تنظیمات پنل‌های VPN برای هر ربات فروشندهٔ VPN (حالت ساخت خودکار).
    هر ربات می‌تواند به چند پنل مختلف متصل باشد؛ panel_key نام یکتای پنل است.
    مقادیر ثابت panel_key: sanai | pasargad | marzban | alireza
    """
    __tablename__ = "vpn_panels"
    __table_args__ = (UniqueConstraint("bot_id", "panel_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    # کلید یکتای پنل — sanai | pasargad | marzban | alireza
    panel_key: Mapped[str] = mapped_column(String(30), default="sanai")
    base_url: Mapped[str] = mapped_column(String(300))
    username: Mapped[str] = mapped_column(String(120))
    password: Mapped[str] = mapped_column(String(200))
    # === PATCH_PANEL_API_KEY_AUTH_V1 ===
    # برخی پنل‌ها (مثلاً پاسارگارد/PasarGuard از نسخهٔ ۵.۱ به بعد) علاوه بر
    # یوزرنیم/پسورد، اتصال با «API Key» ثابت رو هم پشتیبانی می‌کنن. اگه این
    # ستون پر باشه، اتصال با کلید انجام می‌شه (بدون نیاز به لاگینِ یوزر/پس).
    api_key: Mapped[str | None] = mapped_column(String(300))
    sub_base: Mapped[str | None] = mapped_column(String(300))
    inbound_ids: Mapped[str | None] = mapped_column(Text)  # "1,2,3" یا خالی=همه فعال‌ها
    # === PATCH_PANEL_INBOUND_PICKER_V1 ===
    # "all"    = همهٔ اینباندهای فعال؛ با پنل هماهنگ می‌ماند (بروزرسانی خودکار/دستی لیست را بروز می‌کند)
    # "custom" = انتخاب دستی مالک؛ هیچ بروزرسانی‌ای آن را عوض نمی‌کند
    # NULL     = ردیف قدیمی (از روی محتوا حدس زده می‌شود؛ tenant/shared/panel_clients/panel_inbounds.py)
    inbound_mode: Mapped[str | None] = mapped_column(String(10))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # === PATCH_PANEL_AUTOSYNC_V1 ===
    # همگام‌سازیِ خودکارِ اینباندها و ساب‌لینکِ کاربران هر ۲ ساعت. مالکِ ربات
    # از منویِ همین پنل روشن/خاموشش می‌کند. auto_sync_at آخرین اجرای موفق
    # (UTC) است تا حلقهٔ پس‌زمینه بداند نوبتِ کدام پنل رسیده.
    auto_sync: Mapped[bool | None] = mapped_column(Boolean, default=False)
    auto_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    # === PATCH_PANEL_DEFAULT_IP_LIMIT_V1 ===
    # محدودیتِ پیش‌فرضِ تعدادِ کاربرِ هم‌زمان (IP) برایِ کانفیگ‌هایی که رویِ
    # این پنل ساخته می‌شوند. تا امروز فقط «پلن» می‌توانست محدودیت داشته
    # باشد، پس خریدِ «حجم/روزِ دلخواه» — که اصلاً پلنی ندارد — همیشه با
    # عددِ صفر (بدونِ محدودیت) ساخته می‌شد. ۰ یعنی بدونِ محدودیت.
    # فقط پنل‌هایی که این قابلیت را دارند (پاسارگاد/ربکا/شاهان/X-UI)
    # اعمالش می‌کنند؛ بقیه نادیده می‌گیرند.
    default_limit_ip: Mapped[int] = mapped_column(Integer, default=0)
    # === PATCH_PANEL_WATCHDOG_V1 ===
    # وضعیتِ سلامتِ آخرین بررسیِ اتصال (نه autosync — این هر چند دقیقه یک‌بار
    # با test_connection() چک می‌شود). last_health_ok=None یعنی «هنوز هیچ
    # بررسی‌ای انجام نشده». مقصود: تشخیصِ لحظه‌ی تغییرِ وضعیت (سالم→قطع یا
    # برعکس) تا فقط همان لحظه به مالکِ ربات پیام برود، نه هر دور بررسی.
    last_health_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_health_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    # === PATCH_PANEL_WATCHDOG_FLAP_V2 ===
    # نسخهٔ اول به‌محضِ اولین شکستِ چک هشدار می‌داد، پس یک تایم‌اوتِ گذرا یا
    # ری‌استارتِ چنددقیقه‌ایِ پنل باعثِ رگبارِ «قطع شد / وصل شد» می‌شد.
    # حالا سه چیز نگه داشته می‌شود تا فقط قطعیِ *واقعی و پایدار* گزارش شود:
    #   • از کِی پشتِ‌هم خطا می‌دهد (برایِ آستانهٔ زمانی)
    #   • چند بارِ پیاپی خطا داده (برایِ فیلترِ خطایِ تک‌وتوک)
    #   • آیا بابتِ همین قطعی قبلاً هشدار رفته (تا «وصل شد» بی‌مورد نرود)
    health_fail_since: Mapped[datetime | None] = mapped_column(DateTime)
    health_fail_count: Mapped[int] = mapped_column(Integer, default=0)
    health_alerted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class VpnPlan(Base):
    """پلن فروش VPN (حجم/مدت/قیمت) برای هر ربات.
    هر پلن متعلق به یک پنل خاص است (panel_key) — دکمهٔ «پلن‌های فروش» داخل
    منوی مدیریت همان پنل نمایش و فروش داده می‌شود. اگر پنل بعداً حذف شود،
    پلن حذف نمی‌شود و تا غیرفعال‌کردن دستی توسط مالک ربات باقی می‌ماند
    (صرفاً دیگر برای خرید در دسترس نیست چون پنلش وجود ندارد)."""
    __tablename__ = "vpn_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    # کلید پنلی که این پلن به آن تعلق دارد — sanai | pasargad | marzban | alireza
    panel_key: Mapped[str] = mapped_column(String(30), default="sanai", index=True)
    # === PATCH_RESELLER_PLANS_V1 ===
    # مخاطبِ این پلن: all (همه)، users (فقط مشتریِ عادی)، resellers (فقط
    # نماینده). چند مالکِ ربات گزارش دادند تخفیفِ درصدی با ساختارِ قیمتِ
    # نمایندگی‌شان جور درنمی‌آید و منوی جدا می‌خواهند. پیش‌فرض all است تا
    # رفتارِ ربات‌های موجود عوض نشود.
    audience: Mapped[str] = mapped_column(String(10), default="all",
                                          server_default="all")
    title: Mapped[str] = mapped_column(String(100))
    volume_gb: Mapped[int] = mapped_column(Integer)   # 0 = نامحدود
    days: Mapped[int] = mapped_column(Integer)        # 0 = بی‌انقضا
    price: Mapped[float] = mapped_column(Numeric(14, 0))
    limit_ip: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # رنگِ دکمهٔ خرید این پلن در فروشگاه: ""(پیش‌فرض)/danger/success/primary
    color: Mapped[str | None] = mapped_column(String(20), default="")
    # === PATCH_VPN_PLAN_CATEGORY_V1 ===
    # دسته‌بندیِ پلن (مثلاً «پلن حرفه‌ای»/«پلن اقتصادی») — پلن‌هایی که دسته‌بندیِ
    # یکسان دارن، تو لیستِ خرید زیرِ یک تیترِ مشترک کنارِ هم نشون داده می‌شن.
    category: Mapped[str | None] = mapped_column(String(30))
    # ترتیبِ نمایشِ پلن (کمتر = بالاتر)؛ برابر → بر اساسِ id
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # === PATCH_MINIAPP_V1 ===
    # دسته‌بندیِ نمایشی در مینی‌اپ (جدا از فیلدِ متنیِ قدیمیِ category که
    # داخلِ خودِ ربات استفاده می‌شود). nullable است تا پلن‌های قدیمی که هنوز
    # دسته نگرفته‌اند در بخشِ «همه سرورها»ی مینی‌اپ نمایش داده شوند.
    miniapp_category_id: Mapped[int | None] = mapped_column(
        ForeignKey("miniapp_categories.id"), index=True)


# === PATCH_MINIAPP_V1 ===
class MiniAppCategory(Base):
    """دسته‌بندیِ خریدِ دلخواهِ مالکِ ربات برای مینی‌اپ (مثلاً «پنل نامحدود»،
    «پنل حجمی»، «همه سرورها»). هر ربات دسته‌بندی‌های خودش را می‌سازد و هر
    VpnPlan را می‌تواند زیرِ یکی از این دسته‌ها بگذارد."""
    __tablename__ = "miniapp_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    title: Mapped[str] = mapped_column(String(60))
    icon: Mapped[str | None] = mapped_column(String(8), default="🛰")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # === PATCH_MINIAPP_STOCK_CATS_V1 ===
    # دسته‌هایِ انبارِ «کانفیگ آماده» (Config.category) که زیرِ این دستهٔ
    # مینی‌اپ نمایش داده می‌شوند — با کاما جدا می‌شوند. رشتهٔ خالی/None یعنی
    # هیچ کانفیگِ آماده‌ای به این دسته وصل نیست. مقدارِ ویژهٔ "-" یعنی
    # کانفیگ‌هایِ بدونِ دسته‌بندیِ انبار.
    stock_categories: Mapped[str | None] = mapped_column(String(200))
    # === PATCH_CATEGORY_SALE_FULL_V1 === «ظرفیتِ فروش پر است»: دسته در ربات و مینی‌اپ دیده می‌شود
    # ولی خرید ندارد و به کاربر گفته می‌شود از دستهٔ دیگری استفاده کند.
    sale_full: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# === PATCH_VPN_PARTNER_V1 ===
class VpnPartner(Base):
    """اکانت «همکار»: یک کاربرِ نهاییِ ربات (با آیدی عددی تلگرام) که ادمینِ
    ربات به او تخفیفِ درصدیِ دلخواه می‌دهد — بدون نیاز به واردکردنِ کدِ
    تخفیف. لیستِ همکارها مشترکِ کلِ ربات است (نه مختصِ یک پنل)، ولی ادمین
    موقعِ ثبت/ویرایشِ هر همکار مشخص می‌کند این تخفیف رویِ کدام پنل‌های
    متصل و کدام نوعِ خرید (پلن‌های آمادهٔ فروش / فروشِ آنلاینِ حجم‌و‌روزِ
    دلخواه) اعمال شود."""
    __tablename__ = "vpn_partners"
    __table_args__ = (UniqueConstraint("bot_id", "telegram_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    note: Mapped[str | None] = mapped_column(String(80))  # یادداشتِ دلخواه (اسم/توضیح همکار)
    discount_percent: Mapped[int] = mapped_column(Integer, default=30)
    # پنل‌هایی که تخفیف رویشان اعمال می‌شود — رشتهٔ panel_key با کاما
    # (مثلاً "sanai,marzban"). خالی/None یعنی هنوز هیچ پنلی برای این همکار
    # تیک نخورده (نه «همهٔ پنل‌ها») — چون خودِ ادمین صریحاً چک‌لیست می‌کند.
    panel_keys: Mapped[str | None] = mapped_column(Text)
    # === PATCH_PARTNER_PER_PANEL_DISCOUNT_COLUMN_V1 ===
    # درصدِ تخفیفِ اختصاصیِ هر پنل، به‌صورتِ JSON: {"sanai": 16, "marzban": 25}
    # پنلی که کلیدش اینجا نباشد از `discount_percent` (درصدِ کلی) استفاده می‌کند.
    #
    # این ستون در PATCH_PARTNER_PER_PANEL_DISCOUNT_V1 استفاده شده بود ولی
    # هیچ‌وقت به مدل اضافه نشد. چون خواندنش با getattr(p, "panel_discounts",
    # None) بود، خطایی نمی‌داد و همیشه None برمی‌گرداند؛ و نوشتنش فقط یک
    # اَتریبیوتِ معمولیِ پایتون رویِ آبجکت می‌ساخت که commit ذخیره‌اش نمی‌کرد.
    # نتیجه: ادمین درصد را ثبت می‌کرد، پیامِ «✅ تنظیم شد» می‌گرفت، ولی عدد
    # هیچ‌وقت ذخیره نمی‌شد و دوباره صفر/کلی نشان داده می‌شد.
    panel_discounts: Mapped[str | None] = mapped_column(Text)
    # === PATCH_PARTNER_PANEL_GB_PRICE_V1 ===
    # قیمتِ هر گیگِ این همکار رویِ هر پنل (تومان)، JSON: {"pasargad#2": 1000}.
    # اگر برایِ پنلی ثبت شده باشد، *به‌جایِ* درصدِ همان پنل می‌نشیند:
    # پلنِ آماده = حجمِ پلن × این قیمت (حداکثر همان قیمتِ عادی)، فروشِ دلخواه = قیمتِ گیگ.
    panel_gb_prices: Mapped[str | None] = mapped_column(Text)
    apply_plans: Mapped[bool] = mapped_column(Boolean, default=True)    # پلن‌های آمادهٔ فروش
    apply_custom: Mapped[bool] = mapped_column(Boolean, default=False)  # فروش آنلاین دلخواه (حجم/روز آزاد)
    # === PATCH_RESELLER_MERGE_V1 ===
    # «رویِ همهٔ محصولاتِ ربات» — وقتی True باشد، چک‌لیستِ پنل‌ها و دو
    # سوییچِ بالا نادیده گرفته می‌شوند و تخفیف روی هر خریدی اعمال می‌شود.
    # پیش‌فرض False تا رفتارِ همکارهایِ موجود ذره‌ای عوض نشود.
    apply_all: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # === PATCH_RESELLER_WEB_V1 ===
    # دسترسیِ نماینده به «پنل تحت وب». ورود با یوزر/پسورد است چون نماینده
    # لزوماً از تلگرام وارد نمی‌شود. پسورد فقط به‌صورتِ هشِ PBKDF2 ذخیره
    # می‌شود و هیچ‌جا (حتی برای مالکِ ربات) دوباره نمایش داده نمی‌شود.
    web_username: Mapped[str | None] = mapped_column(String(40), index=True)
    # === PATCH_RESELLER_TRIAL_QUOTA_V1 ===
    # سهمیهٔ روزانهٔ تستِ رایگانی که این نماینده می‌تواند برایِ مشتری‌هایش
    # بسازد. ۰ یعنی اصلاً اجازهٔ تست ندارد (پیش‌فرض، تا رفتارِ نماینده‌های
    # موجود عوض نشود). مالکِ ربات برای هر نماینده جدا تعیینش می‌کند، چون
    # نمایندهٔ بزرگ‌تر معمولاً تستِ بیشتری لازم دارد.
    trial_daily_limit: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    web_password_hash: Mapped[str | None] = mapped_column(String(255))
    web_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # ضدِ حدسِ پیاپیِ پسورد
    web_fail_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    web_locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PanelAccount(Base):
    """یک سرویس VPN ساخته‌شدهٔ خودکار روی پنل، متعلق به یک کاربرِ ربات."""
    __tablename__ = "panel_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    email: Mapped[str] = mapped_column(String(120), index=True)  # شناسهٔ یکتای کلاینت در پنل
    # کلید پنلی که این اکانت روی آن ساخته شده (sanai/pasargad/marzban/alireza)
    panel_key: Mapped[str | None] = mapped_column(String(30), default="sanai")
    uuid: Mapped[str | None] = mapped_column(String(80))
    sub_id: Mapped[str | None] = mapped_column(String(80))
    sub_link: Mapped[str | None] = mapped_column(String(400))
    volume_gb: Mapped[int] = mapped_column(Integer, default=0)
    # === PATCH_PANEL_ACCOUNT_VOLUME_MB_V1 ===
    # وقتی حجم کمتر از ۱ گیگ باشه (مثلاً تستِ ۱۰۰ مگابایتی)، volume_gb با
    # int(gb) صفر می‌شه — و صفر توی کل پروژه (اینجا و vpn_user._svc_label)
    # به‌معنیِ «نامحدود» است. یعنی حتی وقتی خودِ پنل درست ۱۰۰ مگ محدودش کرده
    # بود، به کاربر پیام «نامحدود» نشون داده می‌شد. این ستون حجمِ دقیقِ
    # زیرِ ۱ گیگ رو (به مگابایت) جدا نگه می‌داره تا نمایش هم درست باشه؛
    # None یعنی این حساب یا ≥۱ گیگ است یا واقعاً نامحدود.
    volume_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    days: Mapped[int] = mapped_column(Integer, default=0)
    plan_title: Mapped[str | None] = mapped_column(String(100))
    # === PATCH_RESELLER_SEPARATE_CONFIGS_V1 ===
    # کانفیگی که نماینده از پنلِ نمایندگی برایِ مشتری‌اش ساخته، با کانفیگِ
    # شخصیِ خودش قاطی می‌شد. این پرچم آن دو را جدا می‌کند: «سرویس‌های من»
    # فقط شخصی‌ها را نشان می‌دهد و پنلِ نمایندگی فقط ساخته‌هایِ نمایندگی را.
    reseller_made: Mapped[bool] = mapped_column(Boolean, default=False,
                                                server_default="0")
    # کد اختصاصی سرویس (۹ رقمی) — برای نمایش به کاربر و جستجوی مدیریتی
    svc_code: Mapped[str | None] = mapped_column(String(12), index=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    # === PATCH_VPN_REFUND_V1 === مبلغِ واقعاً پرداخت‌شده برایِ همین سرویس (تومان، بعد از تخفیف).
    # NULL = تست رایگان/هدیه/امتیاز/قدیمی → مشمولِ برگشت وجه نیست.
    paid_toman: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # تاریخ انقضای محاسبه‌شده در لحظهٔ ساخت/تمدید (UTC) — برای نمایش در
    # «کانفیگ‌های من» بدون نیاز به فراخوانی زندهٔ پنل؛ منبع حقیقت برای مصرف/حجم
    # همچنان API پنل است، اما این فیلد به‌عنوان fallback نمایشی استفاده می‌شود.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    # تاریخ آخرین اخطار انقضا/حجم (YYYY-MM-DD) تا روزی یک‌بار اطلاع داده شود
    expiry_notice: Mapped[str | None] = mapped_column(String(10))
    # === PATCH_DEPLETED_CLEANUP_V1 ===
    # لحظه‌ای که برایِ اولین‌بار «حجم تمام شده» دیده شد. مبنایِ مهلتِ
    # ۲ روزهٔ تمدید و حذفِ خودکار است. اگر کاربر حجم بخرد و مصرف دوباره
    # زیرِ سقف برود، همین‌جا None می‌شود تا مهلت از نو شمرده شود.
    depleted_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # توکن یکتا برای صفحهٔ وبِ «مشاهدهٔ زندهٔ کانفیگ» (بدون نیاز به ورود)
    web_token: Mapped[str | None] = mapped_column(String(64), index=True)
    # آیا پیام «تبریک اولین اتصال» ارسال شده؟ عمداً بدون مقدار پیش‌فرض:
    # NULL یعنی اکانتِ قدیمی (قبل از این قابلیت) → نادیده گرفته می‌شود (بدون اسپم).
    # False یعنی اکانت تازه‌ساخته‌شده و هنوز اتصال اول رصد نشده → حلقهٔ پس‌زمینه پیگیری می‌کند.
    # True یعنی پیام تبریک قبلاً فرستاده شده.
    first_conn_notified: Mapped[bool | None] = mapped_column(Boolean)
    # === PATCH_PURCHASE_FOLLOWUP_V1 ===
    # آیا پیامِ «پیگیری خرید» (وقتی کاربر بعد از N ساعت هنوز وصل نشده) ارسال شده؟
    # پیش‌فرض False برای اکانت‌های تازه؛ True یعنی یک‌بار پیگیری فرستاده شده و
    # دیگر تکرار نمی‌شود (نه اسپم).
    followup_prompt_sent: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # === PATCH_EXPIRY_WARN_QUOTA_V1 ===
    # شمارندهٔ اخطارهایِ «سرویس منقضی شده» که بعد از تاریخِ انقضا فرستاده شده.
    # هر روز حداکثر یکی (همان قفلِ expiry_notice)، و وقتی به سقفِ تنظیم‌شده
    # (پیش‌فرض ۳) برسد، سرویس هم از پنل و هم از «کانفیگ‌های من» حذف می‌شود.
    # با هر تمدید، هم‌زمان با expiry_notice، صفر می‌شود تا چرخه از نو شروع شود.
    expiry_warn_count: Mapped[int | None] = mapped_column(Integer, default=0)
    # === PATCH_TRIAL_EXPIRE_CLEANUP_V1 ===
    # لحظهٔ ارسالِ «اخطارِ پایانِ تست رایگان». مبنایِ مهلتِ چندساعتهٔ حذفِ
    # کاملِ کانفیگِ تست است (پیش‌فرض ۵ ساعت). None یعنی هنوز اخطار نرفته.
    trial_warned_at: Mapped[datetime | None] = mapped_column(DateTime)
    # === PATCH_TRAFFIC_OUT_CLEANUP_V1 ===
    # لحظه‌ای که برایِ نخستین‌بار دیدیم حجمِ این سرویس تمام شده.
    # تا امروز، «اتمامِ ترافیک» هیچ چرخهٔ پایانی نداشت: نه شمرده می‌شد و
    # نه به حذف می‌رسید — فقط هر روز همان هشدار دوباره می‌رفت (گزارشِ
    # ادمین: «روزِ سومه واسه کاربر ارسال شده»). با این ستون، دقیقاً مثلِ
    # کانفیگِ تست، یک اخطار می‌رود و چند ساعت بعد سرویس حذف می‌شود.
    # NULL یعنی حجم تمام نشده (یا کاربر ترافیک خریده و ریست شده).
    traffic_out_at: Mapped[datetime | None] = mapped_column(DateTime)
    # === PATCH_TRIAL_UNUSED_REMINDER_V1 ===
    # لحظهٔ ارسالِ «هنوز از تستِ رایگانت استفاده نکردی» — وقتی مصرفِ کاربر
    # هنوز صفر است ولی مدتی (trial_unused_hours) از ساختِ تست گذشته.
    # مبنایِ مهلتِ حذفِ خودکارِ تستِ استفاده‌نشده است (با همان بازهٔ
    # trial_delete_hours). None یعنی هنوز این اخطار نرفته.
    trial_unused_warned_at: Mapped[datetime | None] = mapped_column(DateTime)


# === PATCH_MARKETPLACE_V1 ===
# === PATCH_BOT_AUDIT_LOG_V1 ===
class BotAuditLog(Base):
    """سابقهٔ انتقالِ مالکیت و حذفِ ربات‌ها.

    چرا لازم شد: انتقال فقط Bot.owner_id را عوض می‌کرد و حذف کلِ رکورد را
    پاک می‌کرد، پس در پنلِ کاربر هیچ ردی نمی‌ماند که «این ربات به چه کسی
    منتقل شد» یا «چه رباتی حذف شد». این جدول فقط اضافه می‌شود و هیچ
    مسیرِ موجودی را تغییر نمی‌دهد.
    """
    __tablename__ = "bot_audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int | None] = mapped_column(Integer, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    plan: Mapped[str | None] = mapped_column(String(20))
    # transfer | delete
    action: Mapped[str] = mapped_column(String(20), index=True)
    from_owner: Mapped[int | None] = mapped_column(BigInteger, index=True)
    to_owner: Mapped[int | None] = mapped_column(BigInteger, index=True)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# === PATCH_BIG_CHARGE_APPROVAL_V1 ===
class BigChargeRequest(Base):
    """درخواستِ هماهنگیِ واریزِ سنگین.

    بدونِ این جدول، کاربر بعد از دیدنِ پیامِ «هماهنگی» به دایرکت می‌رفت و
    مدیر هیچ سابقه‌ای از مبلغ و هدفِ درخواست نداشت؛ تأیید هم جایی ثبت
    نمی‌شد تا گیت را باز کند.
    """
    __tablename__ = "big_charge_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    amount: Mapped[int] = mapped_column(Integer, default=0)
    # wallet | market
    kind: Mapped[str] = mapped_column(String(20), default="wallet", index=True)
    subject: Mapped[str | None] = mapped_column(String(200))
    # pending | approved | rejected | used
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)


class MarketProject(Base):
    """یک «رباتِ اختصاصیِ آماده» که مدیرِ پلتفرم برایِ فروش می‌گذارد."""
    __tablename__ = "market_projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[int] = mapped_column(Integer, default=0)          # تومان
    # فایلِ zipِ پروژه — file_idِ تلگرام (برایِ تحویلِ مستقیم) و مسیرِ
    # روی دیسک (برایِ فعال‌سازیِ خودکار رویِ VPS).
    zip_file_id: Mapped[str | None] = mapped_column(String(200))
    zip_path: Mapped[str | None] = mapped_column(String(400))
    # file_idِ تصاویرِ پروژه، جدا شده با کاما
    image_ids: Mapped[str | None] = mapped_column(Text)
    # تستِ رایگانِ پیش از خرید (دقیقه). 0 یعنی تست ندارد.
    trial_minutes: Mapped[int] = mapped_column(Integer, default=20)
    # === PATCH_MARKET_ENV_KEYS_V1 ===
    # متغیرهایِ اضافیِ این پروژه، یکی در هر خط به شکلِ KEY=VALUE.
    # مقادیرِ ویژه: {token} و {admin} با ورودیِ کاربر جایگزین می‌شوند.
    env_extra: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MarketPurchase(Base):
    """خرید (یا تستِ) یک پروژه توسطِ یک کاربر."""
    __tablename__ = "market_purchases"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("market_projects.id"), index=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    # download | vps | trial
    kind: Mapped[str] = mapped_column(String(20), default="download", index=True)
    price_paid: Mapped[int] = mapped_column(Integer, default=0)
    # اگر رویِ VPSِ بات‌ساز فعال شده باشد، اپِ ساخته‌شده
    hosted_app_id: Mapped[int | None] = mapped_column(Integer, index=True)
    # اگر رویِ رباتِ خودش نصب کرده باشد، یوزرنیمِ آن ربات
    bot_username: Mapped[str | None] = mapped_column(String(64))
    trial_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    # === PATCH_MARKET_TRIAL_EXPIRY_V1 === لحظه‌ای که حلقهٔ پایانِ تست، ربات را خاموش کرد
    trial_ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CryptoCharge(Base):
    """شارژ کیف پول با ارز دیجیتال (NOWPayments)؛ پس از تأیید IPN شارژ می‌شود."""
    __tablename__ = "crypto_charges"
    __table_args__ = (UniqueConstraint("order_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    order_id: Mapped[str] = mapped_column(String(80), index=True)
    amount_toman: Mapped[float] = mapped_column(Numeric(14, 0))
    usd_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(20), default="waiting")  # waiting/paid
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TonPaysCharge(Base):
    """شارژ کیف پول از طریق درگاه TonPays (کارت‌به‌کارتِ خودکار، مبلغ به تومان).

    نکتهٔ امنیتی: TonPays برایِ کال‌بک هیچ امضایِ رمزنگاری‌شده‌ای (HMAC) ندارد و
    فقط همان API-Key را در هدر پس می‌فرستد. پس بدنهٔ کال‌بک به‌تنهایی هرگز
    ملاکِ شارژ نیست؛ همیشه با فراخوانیِ مستقلِ /invoices/check از خودِ درگاه
    تایید گرفته می‌شود و مبلغ با چیزی که اینجا ثبت شده مقایسه می‌گردد.
    """
    __tablename__ = "tonpays_charges"
    __table_args__ = (UniqueConstraint("order_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    order_id: Mapped[str] = mapped_column(String(20), index=True)   # سقفِ ۲۰ کاراکترِ API
    invoice_id: Mapped[str | None] = mapped_column(String(64), index=True)
    request_amount: Mapped[int] = mapped_column(Integer, default=0)  # مبلغی که خواستیم
    final_amount: Mapped[int] = mapped_column(Integer, default=0)    # مبلغِ یکتا برایِ تطبیق
    status: Mapped[str] = mapped_column(String(20), default="waiting")  # waiting/paid/failed
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# === PATCH_UNIQUEPAY_V1 ===
class UniquePayCharge(Base):
    """شارژ کیف پول از درگاه کارت‌به‌کارتِ یونیک‌پی."""
    __tablename__ = "uniquepay_charges"
    __table_args__ = (UniqueConstraint("hash_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    hash_id: Mapped[str] = mapped_column(String(40), index=True)
    ref_id: Mapped[str | None] = mapped_column(String(64))
    amount_toman: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="waiting")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)


# === PATCH_APAY_GATEWAY_V1 ===
class ApayCharge(Base):
    """شارژ کیف پول با درگاهِ کارت‌به‌کارتِ آنیِ «آپی» (apaygate.shop)."""
    __tablename__ = "apay_charges"
    __table_args__ = (UniqueConstraint("order_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    order_id: Mapped[str] = mapped_column(String(40), index=True)      # شناسهٔ ما (به APay فرستاده می‌شود)
    payment_id: Mapped[str | None] = mapped_column(String(64), index=True)   # pay_… از APay
    amount_toman: Mapped[int] = mapped_column(Integer, default=0)      # مبلغی که کیف پول شارژ می‌شود
    # === PATCH_APAY_FEE_PAYER_V1 === کارمزدی که خریدار رویِ فاکتور می‌دهد (۰ = به‌عهدهٔ فروشنده)
    fee_toman: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    pay_amount_rial: Mapped[int | None] = mapped_column(BigInteger)    # مبلغِ دقیقِ واریز (با رقمِ یکتا)
    pay_url: Mapped[str | None] = mapped_column(String(300))
    chat_id: Mapped[int | None] = mapped_column(BigInteger)   # پیامِ فاکتور؛ بعد از پرداخت پاک می‌شود
    msg_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), default="waiting")   # waiting/paid/cancelled/expired
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    # === PATCH_APAY_ADMIN_ASK_V1 === پرسشِ «واریز شد؟» از ادمین بعد از ۵ دقیقه (یک‌بار)
    admin_asked_at: Mapped[datetime | None] = mapped_column(DateTime)
    # === PATCH_APAY_ASK_CLEANUP_V1 === پیامِ «واریز شد؟»ی ادمین؛ با تسویهٔ خودکار پاک می‌شود
    admin_ask_chat: Mapped[int | None] = mapped_column(BigInteger)
    admin_ask_msg: Mapped[int | None] = mapped_column(BigInteger)
    # === PATCH_APAY_DIRECT_PURCHASE_V1 === پرداختِ مستقیمِ سفارش/پلن/خریدِ دلخواه (همان رمزگذاریِ آبان‌گیت‌وی):
    # >۰ شمارهٔ سفارش، <۰ پلنِ VPN (−plan_id) یا خریدِ دلخواه (vc_pack)
    for_order_id: Mapped[int | None] = mapped_column(Integer, index=True)


# === PATCH_ABANGATEWAY_V1 ===
class AbanGatewayCharge(Base):
    """شارژ کیف پول از درگاه کارت‌به‌کارتِ خودکارِ «آبان گیت‌وی» (abangateway.ir).

    order_id: شناسهٔ یکتایِ ما (چیزی که به API به‌عنوانِ order_id می‌فرستیم و
    از آن برمی‌گردیم — قبل از ساختِ فاکتورِ واقعی هم می‌شود ساخته باشد).
    invoice_id: شناسهٔ فاکتور روی خودِ آبان گیت‌وی (بعدِ موفقیتِ create پر می‌شود).
    amount_toman: مبلغی که کاربر خواسته و همان مبلغی که به کیفِ پول اضافه
    می‌شود. payable_toman: مبلغِ دقیقی که آبان گیت‌وی از کاربر خواسته واریز
    کند (چند ریال بیشتر، برایِ تشخیصِ خودکار از رویِ پیامکِ بانک) — این دو
    عمداً جدا نگه داشته شده‌اند تا کیفِ پول هیچ‌وقت با آن چند ریالِ اضافه
    شارژ نشود.
    """
    __tablename__ = "abangateway_charges"
    __table_args__ = (UniqueConstraint("order_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    order_id: Mapped[str] = mapped_column(String(40), index=True)
    invoice_id: Mapped[str | None] = mapped_column(String(64), index=True)
    ref_id: Mapped[str | None] = mapped_column(String(64))
    amount_toman: Mapped[int] = mapped_column(Integer, default=0)
    payable_toman: Mapped[int] = mapped_column(Integer, default=0)
    card_number: Mapped[str | None] = mapped_column(String(32))
    card_holder: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default="waiting")
    # === PATCH_ABANGATEWAY_CLAIM_ALERT_V1 ===
    # وقتی کاربر رویِ «بررسیِ پرداخت» می‌زند ولی هنوز از درگاه تأییدی نیامده،
    # اینجا ثبت می‌شود تا اگر بعدِ چند دقیقه هنوز waiting مانده بود، به ادمین
    # هشدار بدهیم (فقط یک‌بار — admin_notified از تکرارِ هشدار جلوگیری می‌کند).
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)
    admin_notified: Mapped[bool] = mapped_column(Boolean, default=False)
    # === PATCH_GATEWAY_PAY_FOR_ORDER_V1 ===
    # اگر این شارژ از دلِ «پرداختِ محصول» شروع شده باشد، شمارهٔ همان سفارش
    # اینجا می‌ماند تا بعد از تأیید، مستقیم به تسویهٔ همان سفارش برگردیم.
    # صفر/خالی یعنی شارژِ معمولیِ کیف‌پول.
    for_order_id: Mapped[int | None] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)


class HesabPayCharge(Base):
    """فاکتور پرداخت افغانیِ HesabPay برای شارژ اعتبار تومانی tenant."""
    __tablename__ = "hesabpay_charges"
    __table_args__ = (UniqueConstraint("reference"), UniqueConstraint("transaction_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    amount_toman: Mapped[float] = mapped_column(Numeric(14, 0))
    amount_afn: Mapped[float] = mapped_column(Numeric(14, 2))
    rate_toman_per_afn: Mapped[float] = mapped_column(Numeric(14, 2))
    reference: Mapped[str] = mapped_column(String(80), index=True)
    session_id: Mapped[str | None] = mapped_column(String(120))
    checkout_url: Mapped[str | None] = mapped_column(String(1000))
    transaction_id: Mapped[str | None] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(20), default="creating", index=True)
    mode: Mapped[str] = mapped_column(String(10), default="sandbox")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)


class TonDirectCharge(Base):
    """شارژ کیف پول با واریزِ مستقیمِ TON به ولتِ خودِ مالکِ ربات.

    === PATCH_TON_DIRECT_V1 ===
    برخلافِ TonPaysCharge اینجا هیچ درگاه/واسطه‌ای وجود ندارد: کاربر مستقیم
    به آدرسِ ولتِ فروشنده (مثلاً Tonkeeper) واریز می‌کند و ما پرداخت را از
    رویِ خودِ بلاک‌چین تشخیص می‌دهیم. دو راهِ تشخیص، به‌ترتیبِ اولویت:
      ۱) memo/کامنتِ یکتا که در لینکِ پرداخت از قبل پر می‌شود
      ۲) مبلغِ یکتا (چند رقمِ آخرِ اعشارِ TON تصادفی است) — برایِ وقتی که
         کاربر از صرافی برداشت کرده و کامنت حذف شده است
    tx_hash یکتاست تا یک تراکنشِ واحد هرگز دوبار شارژ نشود.
    """
    __tablename__ = "ton_direct_charges"
    __table_args__ = (UniqueConstraint("memo", name="uq_tondirect_memo"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    memo: Mapped[str] = mapped_column(String(24), index=True)
    # === PATCH_COIN_DIRECT_MULTI_ASSET_V1 ===
    # همین جدول حالا فاکتورهایِ TRX و USDT (TRC20) را هم نگه می‌دارد؛
    # ماژولِ tenant/tron_direct.py مالکِ آن ردیف‌هاست. عمداً nullable است تا
    # _migrate_new_columns بتواند رویِ دیتابیسِ موجود اضافه‌اش کند —
    # ردیف‌هایِ قدیمی مقدارِ NULL دارند و همه‌جا «TON» تفسیر می‌شوند.
    asset: Mapped[str | None] = mapped_column(String(10), index=True)  # TON/TRX/USDT
    amount_toman: Mapped[int] = mapped_column(Numeric(14, 0), default=0)
    # مبلغِ دقیقِ ارز که به کاربر اعلام شده (با اعشارِ یکتاکننده)
    ton_amount: Mapped[float] = mapped_column(Numeric(18, 9), default=0)
    # نرخِ TON به تومان در لحظهٔ ساختِ فاکتور — برایِ پیگیریِ بعدی
    rate_toman: Mapped[int] = mapped_column(Numeric(14, 0), default=0)
    wallet_address: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default="waiting")  # waiting/paid/expired
    tx_hash: Mapped[str | None] = mapped_column(String(80), index=True)
    # === PATCH_TON_DIRECT_AUTODELETE_INVOICE_V1 ===
    # پیامِ فاکتور را نگه می‌داریم تا بعدِ تاییدِ پرداخت (چه خودکار توسطِ
    # حلقه، چه با دکمهٔ «بررسی وضعیت») حذفش کنیم — دیگر لازم نیست کاربر
    # پیامِ کهنهٔ فاکتور را در چت نگه دارد.
    invoice_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    invoice_message_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)


class StockRequest(Base):
    """درخواست کاربر برای بسته‌ای که موجود نیست (لیست انتظار موجودی)."""
    __tablename__ = "stock_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    volume_gb: Mapped[int] = mapped_column(Integer)
    days: Mapped[int] = mapped_column(Integer)
    fulfilled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class GatedClaim(Base):
    """دریافتِ جایزهٔ یک دکمهٔ «شرطی‌سازی‌شده با زیرمجموعه» توسط یک کاربر.

    وجود رکورد یعنی این کاربر جایزهٔ این دکمه را گرفته است؛ UniqueConstraint روی
    (button_id, user_id) تضمین می‌کند هر کاربر فقط یک‌بار جایزه بگیرد (و در
    دفعات بعد همان محتوای دریافتی دوباره نمایش داده می‌شود، نه جایزهٔ جدید).
    """
    __tablename__ = "gated_claims"
    # قید یکتا روی (button_id, user_id, tier): هر کاربر هر «سطح» جایزه را فقط یک‌بار
    # می‌گیرد. tier=0 یعنی جایزهٔ تک‌سطحی/کلاسیک؛ tier>=1 یعنی جایزهٔ پلکانیِ آن سطح.
    __table_args__ = (
        UniqueConstraint("button_id", "user_id", "tier",
                         name="uq_gated_claims_btn_user_tier"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    button_id: Mapped[int] = mapped_column(ForeignKey("main_buttons.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    tier: Mapped[int] = mapped_column(Integer, default=0)   # شمارهٔ سطحِ جایزهٔ پلکانی (0=کلاسیک)
    delivered: Mapped[str | None] = mapped_column(Text)   # snapshot محتوای تحویل‌شده (کانفیگ/متن)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# === PATCH_GATE_DEDICATED_LINK_V1 ===
class GateReferralLink(Base):
    """لینکِ دعوتِ اختصاصیِ هر (کاربر, دکمهٔ گیت‌شده) برایِ حالتِ src=«gatelink»
    در «گیتِ دسترسیِ زیرمجموعه‌ای». کاملاً مستقل از لینکِ دعوتِ عمومیِ کاربر
    (ReferralLink) و از بقیهٔ دکمه‌های گیت‌شده — یعنی رفقایی که از این لینک
    عضو می‌شن فقط برایِ همین دکمه شمارش می‌شن. join_count هر بار که کاربر
    با موفقیت از دکمه استفاده کند صفر می‌شود تا برایِ دفعهٔ بعد مجبور شود
    از نو، رفقایِ تازه (نه قبلی) بیاورد."""
    __tablename__ = "gate_referral_links"
    __table_args__ = (
        UniqueConstraint("button_id", "user_id", name="uq_gate_ref_btn_user"),
        UniqueConstraint("code", name="uq_gate_ref_code"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    button_id: Mapped[int] = mapped_column(ForeignKey("main_buttons.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    code: Mapped[str] = mapped_column(String(24), index=True)
    join_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class GateReferralJoin(Base):
    """جلوگیری از شمارشِ تکراریِ همون فردِ دعوت‌شده برایِ همون لینکِ گیتِ
    اختصاصی — دقیقاً مثلِ ReferralJoin برایِ سیستمِ رفرالِ عمومی."""
    __tablename__ = "gate_referral_joins"
    __table_args__ = (
        UniqueConstraint("link_id", "referred_telegram_id", name="uq_gate_ref_join"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    link_id: Mapped[int] = mapped_column(ForeignKey("gate_referral_links.id"), index=True)
    referred_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# === PATCH_REFERRAL_POINTS_V1 ===
class RefPointWallet(Base):
    """کیفِ «امتیازِ رفرال» هر کاربر در هر ربات.

    هر زیرمجموعه‌ای که کاربر بیاورد به‌اندازهٔ refpoint_per_join امتیاز می‌گیرد
    و امتیازها جمع می‌شوند. دکمه‌هایی که «گیتِ دسترسی با امتیاز» رویشان تنظیم
    شده، در هر استفاده به‌اندازهٔ آستانه از points کم می‌کنند (پس ۳ امتیاز =
    یک‌بار، ۶ امتیاز = دو‌بار، …). total_earned فقط برای گزارش است و کم نمی‌شود.
    """
    __tablename__ = "ref_point_wallets"
    __table_args__ = (
        UniqueConstraint("bot_id", "user_id", name="uq_refpoint_bot_user"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    points: Mapped[int] = mapped_column(Integer, default=0)
    total_earned: Mapped[int] = mapped_column(Integer, default=0)
    # === PATCH_REFPOINT_REWARD_TOPUP_V1 ===
    # اکانتِ جایزهٔ «کانفیگِ خودکار» این کاربر. بارِ اول ساخته می‌شود و
    # دفعاتِ بعد به‌جایِ ساختِ کانفیگِ تازه، حجمِ همین اکانت شارژ می‌شود.
    # (جایزهٔ «انبار» شارژ نمی‌شود و هر بار یک کانفیگِ جدا تحویل می‌دهد.)
    reward_account_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# === PATCH_COMMUNITY_MGMT_V1 ===
class CommunityChat(Base):
    """یک گروه/کانالی که مالکِ رباتِ «مدیریتِ گروه و کانال» به رباتش وصل کرده.

    وقتی ربات به‌عنوانِ ادمین به یک گروه/کانال اضافه می‌شود، این ردیف
    خودکار ساخته می‌شود. تنظیماتِ هر قابلیت (ضداسپم/خوشامد/…) مستقیم روی
    همین ردیف نشسته‌اند تا هر چت تنظیماتِ جدایِ خودش را داشته باشد.
    """
    __tablename__ = "community_chats"
    __table_args__ = (UniqueConstraint("bot_id", "chat_id", name="uq_community_chat"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_type: Mapped[str] = mapped_column(String(20))    # group / supergroup / channel
    title: Mapped[str | None] = mapped_column(String(200))
    username: Mapped[str | None] = mapped_column(String(64))
    added_by: Mapped[int | None] = mapped_column(BigInteger)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # ضداسپم
    antispam_on: Mapped[bool] = mapped_column(Boolean, default=False)
    antispam_links: Mapped[bool] = mapped_column(Boolean, default=True)
    antispam_forwards: Mapped[bool] = mapped_column(Boolean, default=False)
    antispam_duplicate: Mapped[bool] = mapped_column(Boolean, default=True)
    antispam_action: Mapped[str] = mapped_column(String(10), default="delete")  # delete/warn/mute

    # خوشامدگویی
    welcome_on: Mapped[bool] = mapped_column(Boolean, default=False)
    welcome_text: Mapped[str | None] = mapped_column(Text)
    welcome_delete_service_msg: Mapped[bool] = mapped_column(Boolean, default=True)
    welcome_delete_after_sec: Mapped[int] = mapped_column(Integer, default=0)  # ۰=هرگز

    # فیلترِ کلمات
    filter_on: Mapped[bool] = mapped_column(Boolean, default=False)
    filter_action: Mapped[str] = mapped_column(String(10), default="delete")  # delete/warn/mute

    # === PATCH_COMMUNITY_STATS_V1 ===
    # شمارنده‌هایِ ساده برایِ صفحهٔ آمار — از زمانِ اتصال یا آخرین صفرشدن.
    stat_antispam_deleted: Mapped[int] = mapped_column(Integer, default=0)
    stat_filter_deleted: Mapped[int] = mapped_column(Integer, default=0)
    stat_warns_issued: Mapped[int] = mapped_column(Integer, default=0)
    stat_bans_issued: Mapped[int] = mapped_column(Integer, default=0)
    stat_welcomed: Mapped[int] = mapped_column(Integer, default=0)
    stats_since: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CommunityWarn(Base):
    """شمارندهٔ اخطارِ هر عضو در هر چتِ متصل (برایِ ضداسپم/فیلترِ کلمات/دستیِ ادمین)."""
    __tablename__ = "community_warns"
    __table_args__ = (UniqueConstraint("chat_row_id", "user_id", name="uq_community_warn"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_row_id: Mapped[int] = mapped_column(ForeignKey("community_chats.id"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now())


class CommunityFilterWord(Base):
    """کلماتِ ممنوعِ هر چت — پیامِ حاویِ این کلمات طبقِ filter_action رسیدگی می‌شود."""
    __tablename__ = "community_filter_words"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_row_id: Mapped[int] = mapped_column(ForeignKey("community_chats.id"), index=True)
    word: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ═══════════════════════════════════════════
#  شماره‌هایِ واقعیِ شمارهٔ مجازی — PATCH_VNUM_REAL_NUMBERS_V1
# ═══════════════════════════════════════════
# قبلاً «کشور» فقط یک ردیفِ SmmService با یک قیمتِ ثابت بود — یعنی
# مشتری فقط می‌دید «آمریکا موجود است» ولی هیچ شمارهٔ واقعی‌ای در کار
# نبود؛ ادمین بعدِ تاییدِ سفارش باید دستی و بیرون از ربات شماره را
# می‌فرستاد. حالا هر «کشور» (همان SmmService) می‌تواند چندین شمارهٔ
# واقعی داشته باشد — مشتری وقتی کشور را باز می‌کند، فهرستِ خودِ
# شماره‌ها (موجود/فروخته‌شده) را می‌بیند، یکی را انتخاب و پرداخت
# می‌کند، و بلافاصله همان شماره را در ربات دریافت می‌کند — بدونِ نیازِ
# به پیامِ خصوصیِ جداگانه از ادمین.
class VnumNumber(Base):
    __tablename__ = "vnum_numbers"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    country_service_pk: Mapped[int] = mapped_column(ForeignKey("smm_services.id"), index=True)
    phone_number: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(12), default="available")  # available/sold
    sold_to_user_pk: Mapped[int | None] = mapped_column(ForeignKey("bot_users.id"))
    sold_order_pk: Mapped[int | None] = mapped_column(ForeignKey("smm_orders.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    sold_at: Mapped[datetime | None] = mapped_column(DateTime)


# ═══════════════════════════════════════════
#  گیفتِ تلگرام — PATCH_TELEGRAM_GIFTS_V1
# ═══════════════════════════════════════════

class TelegramGiftConfig(Base):
    """تنظیماتِ ادمین برایِ هر گیفتِ رسمیِ تلگرام. خودِ فهرستِ گیفت‌ها از
    Bot API (getAvailableGifts) می‌آید — اینجا فقط فعال/غیرفعال‌بودنش برایِ
    فروش در همین ربات ذخیره می‌شود.

    === PATCH_MANUAL_GIFT_V1 ===
    اگر is_manual=True باشد، این ردیف اصلاً از کاتالوگِ تلگرام نیامده —
    خودِ ادمین با یک لینک (مثلاً یک گیفتِ منحصربه‌فرد/NFT) دستی اضافه‌اش
    کرده. چون این‌جور گیفت‌ها را ربات نمی‌تواند خودکار بفرستد (نیازِ به
    اتصالِ بیزنس‌اکانتِ تلگرام دارد که کاری کاملاً جداست)، سفارششان همیشه
    برایِ تحویلِ دستیِ ادمین می‌رود، نه sendGiftِ خودکار.
    """
    __tablename__ = "telegram_gift_configs"
    __table_args__ = (UniqueConstraint("bot_id", "gift_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    gift_id: Mapped[str] = mapped_column(String(40))
    star_count: Mapped[int] = mapped_column(Integer, default=0)  # آخرین قیمتِ استارزی که از تلگرام خواندیم
    emoji: Mapped[str | None] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now())
    # === PATCH_MANUAL_GIFT_V1 ===
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    # === PATCH_SMM_UNIT_MODE_V1 ===
    # استانداردِ SMM قیمت را «به‌ازای هر ۱۰۰۰» می‌دهد، ولی بعضی سرویس‌ها
    # تکی‌اند (گیفت، اکانت، …) و عددِ rate قیمتِ *یک عدد* است. تا قبل از
    # این، ربات همه را هزارتایی فرض می‌کرد و قیمتِ سرویسِ تکی بر ۱۰۰۰
    # تقسیم می‌شد — یعنی ۸۴٬۵۰۰ تومان به ۸۴ تومان فروخته می‌شد.
    #   auto      → از رویِ max_qty تشخیص بده (پیش‌فرض)
    #   per_1000  → قیمت به‌ازای هر ۱۰۰۰ عدد
    #   per_unit  → قیمت به‌ازای هر عدد
    unit_mode: Mapped[str] = mapped_column(String(10), default="auto")
    manual_link: Mapped[str | None] = mapped_column(String(300))
    manual_name: Mapped[str | None] = mapped_column(String(150))
    manual_price_toman: Mapped[int | None] = mapped_column(Integer)


class GiftOrder(Base):
    """تاریخچهٔ خریدِ گیفت — برایِ گزارش و پیگیریِ سفارش‌هایی که در ارسال
    شکست خوردند (کمبودِ موجودیِ استارزِ خودِ ربات)."""
    __tablename__ = "gift_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    buyer_user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    gift_id: Mapped[str] = mapped_column(String(40))
    gift_emoji: Mapped[str | None] = mapped_column(String(20))
    star_count: Mapped[int] = mapped_column(Integer)
    price_toman: Mapped[int] = mapped_column(Integer)
    dest_telegram_id: Mapped[int] = mapped_column(BigInteger)
    dest_label: Mapped[str] = mapped_column(String(120))  # برایِ نمایش (خودم/یوزرنیم)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/sent/failed/refunded
    fail_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # === PATCH_ORDER_TRACKING_V1 ===
    tracking_code: Mapped[str | None] = mapped_column(String(20), index=True)
    # === PATCH_GIFT_CARD_PAYMENT_V1 ===
    pay_method: Mapped[str] = mapped_column(String(10), default="wallet")  # wallet/card
    receipt_photo_id: Mapped[str | None] = mapped_column(String(300))
    # === PATCH_CARD_LAST4_V1 ===
    # چهار رقمِ آخرِ کارتی که مشتری از آن پرداخت کرده — فقط برایِ
    # پرداختِ کارت‌به‌کارت پر می‌شود (کیف‌پول اصلاً از این مسیر رد نمی‌شود)،
    # تا ادمین موقعِ تطبیقِ رسید با گردشِ حساب راحت‌تر تشخیص بدهد.
    payer_card_last4: Mapped[str | None] = mapped_column(String(4))
    # === PATCH_GIFT_QUANTITY_V1 ===
    # قبلاً هر سفارشِ گیفت همیشه دقیقاً یک عدد بود. حالا مشتری می‌تواند
    # قبل از پرداخت تعداد را انتخاب کند (مثلِ نمونهٔ رقبا با دکمه‌های -/+).
    # star_count/price_toman همچنان قیمتِ «هر واحد» را نگه می‌دارند —
    # مبلغِ نهایی = price_toman × quantity.
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    # === PATCH_GIFT_USER_CHOICE_V1 ===
    # روشِ تحویلی که خودِ مشتری انتخاب کرده: "" (پیش‌فرض/طبقِ تنظیماتِ کلی) | "bot" | "support".
    # sent_qty: چند عددش تا حالا واقعاً فرستاده شده (برایِ سفارش‌هایِ منتظرِ موجودیِ استارزِ ربات).
    deliver_via: Mapped[str] = mapped_column(String(10), default="", server_default="")
    sent_qty: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # === PATCH_GIFT_COMMENT_V1 === کامنتِ اختیاریِ خریدار که همراهِ گیفت فرستاده می‌شود (حداکثر ۱۲۸ حرف)
    comment: Mapped[str | None] = mapped_column(String(255))
    # === PATCH_GIFT_ANON_SCOPE_V1 ===
    # anonymous: مشتری خواسته نامش مخفی بماند (فقط با تحویلِ دستی ممکن است).
    # comment_scope: 0 = کامنت برایِ همهٔ گیفت‌ها، N = فقط گیفتِ شمارهٔ N.
    anonymous: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    comment_scope: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # === PATCH_VS_DISCOUNT_V1 ===
    # کدِ تخفیفِ مصرف‌شده برایِ این سفارش (DiscountCode.id) و مبلغِ تخفیف؛ برایِ برگرداندنِ مصرفِ کد
    # وقتی ادمین سفارش را رد می‌کند. NULL = بدونِ کد.
    discount_code_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discount_amount: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class ButtonSlide(Base):
    """یک اسلاید از اسلایدشو یک دکمه اصلی."""
    __tablename__ = "button_slides"

    id: Mapped[int] = mapped_column(primary_key=True)
    button_id: Mapped[int] = mapped_column(ForeignKey("main_buttons.id"), index=True)
    step_num: Mapped[int] = mapped_column(Integer, default=1)
    caption: Mapped[str | None] = mapped_column(Text)
    media_file_id: Mapped[str | None] = mapped_column(String(200))
    media_type: Mapped[str | None] = mapped_column(String(20))  # 'photo'|'video'|'document'|None
    btn_url: Mapped[str | None] = mapped_column(Text)            # لینک دکمه شیشه‌ای
    btn_label: Mapped[str | None] = mapped_column(String(100))   # برچسب دکمه لینک
    buy_product_id: Mapped[int | None] = mapped_column(Integer)  # آیدی محصول برای دکمه خرید
    # === PATCH_SLIDE_ADMIN_NAME_V1 ===
    # اسمِ داخلیِ اسلاید، فقط برای خودِ ادمین (به کاربر نشون داده نمی‌شه) —
    # تا تو لیستِ اسلایدها فوری بفهمه این اسلاید چیه، بدونِ باز کردنش.
    admin_name: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# === PATCH_SLIDESHOW_ENHANCEMENTS_V1 ===
class SlideFavorite(Base):
    """علاقه‌مندی‌های کاربر روی اسلایدهای یک اسلایدشو — یک ردیف یعنی این
    کاربر این اسلایدِ خاص را ⭐ کرده."""
    __tablename__ = "slide_favorites"
    __table_args__ = (UniqueConstraint("user_telegram_id", "slide_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    button_id: Mapped[int] = mapped_column(ForeignKey("main_buttons.id"), index=True)
    slide_id: Mapped[int] = mapped_column(ForeignKey("button_slides.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TokenSwapRequest(Base):
    """درخواست تعویض توکن ربات — وقتی مالک ربات تلگرامی تازه‌ای می‌سازد و
    می‌خواهد اطلاعات ربات قدیمش به ربات جدید منتقل شود."""
    __tablename__ = "token_swap_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    old_bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    new_bot_id: Mapped[int | None] = mapped_column(ForeignKey("bots.id"))
    new_token: Mapped[str | None] = mapped_column(String(120))
    new_username: Mapped[str | None] = mapped_column(String(64))
    new_tg_id: Mapped[int | None] = mapped_column(BigInteger)
    requested_by: Mapped[int] = mapped_column(BigInteger)
    # pending_token: منتظر ارسال توکن جدید توسط مالک ربات
    # pending_admin: توکن دریافت شد، منتظر تایید ادمین پلتفرم
    # approved / rejected
    status: Mapped[str] = mapped_column(String(20), default="pending_token")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)


class OwnerTransferRequest(Base):
    """درخواست انتقال یک ربات به مالک تلگرامی دیگر — فقط ادمین پلتفرم می‌تواند آغاز کند
    (از پنل مدیریت ربات‌ها). سه حالت پشتیبانی می‌شود:
    ۱) همان توکن فعلی بماند و فقط owner_id عوض شود (نیازی به این جدول نیست).
    ۲) ادمین خودش توکن جدید را وارد کند (نیازی به این جدول نیست).
    ۳) از کاربر جدید خواسته شود توکن ربات خودش را همینجا بفرستد — این حالت از
    این جدول برای نگه‌داشتن وضعیت درخواست بین ادمین و کاربر جدید استفاده می‌کند."""
    __tablename__ = "owner_transfer_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    old_bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    requested_by: Mapped[int] = mapped_column(BigInteger)
    new_owner_id: Mapped[int] = mapped_column(BigInteger)
    new_bot_id: Mapped[int | None] = mapped_column(ForeignKey("bots.id"))
    new_token: Mapped[str | None] = mapped_column(String(120))
    new_username: Mapped[str | None] = mapped_column(String(64))
    # pending_token: منتظر ارسال توکن توسط مالک جدید / done / cancelled
    status: Mapped[str] = mapped_column(String(20), default="pending_token")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)


# ---------- جداول متعلق به هر ربات مستأجر ----------
# نکته: PlanOrder عمداً حذف نمی‌شود تا سوابق مالی/خرید باقی بماند (به‌جایش target_bot_id آن NULL می‌شود).


# ======================= انجمن بات‌ساز =======================

class CommunityButton(Base):
    """دکمه‌های انجمن بات‌ساز — هر دکمه یک آموزش مرحله‌به‌مرحله دارد."""
    __tablename__ = "community_buttons"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    color: Mapped[str | None] = mapped_column(String(20))   # danger / success / primary
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CommunityStep(Base):
    """یک مرحله از آموزش یک دکمه انجمن."""
    __tablename__ = "community_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    button_id: Mapped[int] = mapped_column(ForeignKey("community_buttons.id"), index=True)
    step_num: Mapped[int] = mapped_column(Integer)
    text: Mapped[str | None] = mapped_column(Text)
    photo_file_id: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PlatformFeedback(Base):
    """نظرات، باگ‌ها و پیشنهادات ارسال‌شده توسط کاربران بات‌ساز."""
    __tablename__ = "platform_feedbacks"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_name: Mapped[str | None] = mapped_column(String(120))
    fb_type: Mapped[str] = mapped_column(String(10), index=True)  # bug / suggest / review
    text: Mapped[str] = mapped_column(Text)
    admin_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# === PATCH_SMM_MULTI_PANEL_V1 ===
class SmmPanel(Base):
    """یک پنلِ SMM متصلِ به یک ربات. هر ربات می‌تواند چند پنل داشته باشد
    (مثلاً یکی برای ممبر/ویو، یکی دیگر برای شمارهٔ مجازی یا قیمتِ بهتر).

    برایِ سازگاری با ربات‌هایی که قبل از این فیچر یک پنل تنها با
    Setting.smm_api_url/smm_api_key وصل کرده بودند، آن تنظیمات دست‌نخورده
    می‌مانند و به‌عنوانِ «پنلِ پیش‌فرض» (panel_pk=NULL روی SmmService/SmmOrder)
    ادامه می‌دهند؛ این جدول فقط پنل‌هایِ *اضافه‌ی* بعدی را نگه می‌دارد.
    """
    __tablename__ = "smm_panels"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    api_url: Mapped[str] = mapped_column(String(255))
    api_key: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # === PATCH_SMM_BULK_STATUS_V1 ===
    # بعضی پنل‌ها اکشنِ status رو با پارامترِ جمعیِ orders (چند شناسه با
    # کاما) هم پشتیبانی می‌کنن — یعنی به‌جایِ N درخواستِ جدا برایِ N
    # سفارشِ باز، توی یک درخواست وضعیتِ همه رو برمی‌گردونن. چون همه‌ی
    # پنل‌ها این رو ندارن، پیش‌فرض خاموشه؛ ادمین دستی روشنش می‌کنه.
    bulk_status: Mapped[bool] = mapped_column(Boolean, default=False)
    # === PATCH_SMM_PER_PANEL_SETTINGS_V1 ===
    # قبلاً این تنظیمات سطحِ کلِ ربات بودن (یک margin/rate/order_mode/
    # fail_policy برایِ همهٔ پنل‌ها با هم) — منطقی نبود چون هر پنل ممکنه
    # قیمتِ پایه/کیفیت/سرعتِ متفاوتی داشته باشه. حالا هر پنل تنظیماتِ
    # خودش رو جدا داره. پنلِ پیش‌فرضِ قدیمی (panel_pk=NULL) همچنان از
    # همون Settingهایِ سطحِ رباتِ قبلی می‌خونه (دست‌نخورده، سازگار).
    price_mode: Mapped[str] = mapped_column(String(10), default="margin")
    margin_pct: Mapped[float] = mapped_column(Numeric(6, 2), default=30)
    rate_mode: Mapped[str] = mapped_column(String(10), default="manual")
    toman_per_unit: Mapped[float] = mapped_column(Numeric(14, 4), default=0)
    # === PATCH_SMM_PANEL_CURRENCY_V1 ===
    # واحدِ پولیِ panel_rate: "usd" یا "toman". قبلاً چنین فیلدی وجود
    # نداشت و در حالتِ rate_mode=auto همیشه فرض می‌شد قیمتِ پنل دلاری
    # است، پس نرخِ زندهٔ دلار همیشه ضرب می‌شد — حتی برایِ پنل‌هایِ
    # تومانی. نتیجه یک تورمِ چند صدهزار برابری در قیمتِ نهایی بود.
    currency: Mapped[str] = mapped_column(String(10), default="usd")
    order_mode: Mapped[str] = mapped_column(String(10), default="manual")
    fail_policy: Mapped[str] = mapped_column(String(20), default="auto_refund")
    # === PATCH_SMM_PRICE_ROUND_V1 ===
    # رندِ قیمتِ محاسبه‌شده — "0" یعنی رند نشه، وگرنه قیمت به نزدیک‌ترین
    # مضربِ این عدد (تومان) گرد می‌شه (مثلاً 100 یعنی رندِ صدگان).
    price_round: Mapped[int] = mapped_column(Integer, default=0)
    # === PATCH_SMM_PROVIDER_DETAIL_V1 ===
    # برایِ صفحهٔ عملیاتِ «ارائه‌دهنده» (references/smm-menu-redesign.md در
    # اسکیلِ botsaz-dev): وضعیتِ اتصال/موجودی کش می‌شود تا لیست بدونِ زدنِ
    # زندهٔ API به هر پنل رندر شود؛ بررسیِ خودکار با
    # main_tenant.smm_panel_health_loop تازه نگهش می‌دارد.
    auto_check_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_check_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    last_balance: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True, default=None)
    last_currency: Mapped[str] = mapped_column(String(20), default="")


# === PATCH_SMM_MANUAL_CATEGORY_V1 ===
class SmmCategory(Base):
    """دستهٔ ساخته‌شده به‌دستِ ادمین — برخلافِ دسته‌بندیِ خودکار (که از رویِ
    اسمِ سرویس حدس می‌زند)، این‌جا ادمین خودش پلتفرم و اسمِ دسته را
    مشخص می‌کند (مشابهِ ساختاری که رقبا استفاده می‌کنن: تلگرام > «خدماتِ
    گروهِ تلگرام»، با سودِ مخصوصِ خودِ همون دسته)."""
    __tablename__ = "smm_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    platform: Mapped[str] = mapped_column(String(20))  # کلیدِ پلتفرم، مثلِ smm_api.PLATFORMS
    name: Mapped[str] = mapped_column(String(60))
    # === اگر خالی بمونه، سودِ محصول/پنل به‌جاش استفاده می‌شه (اولویت: محصول > دسته > پنل) ===
    margin_pct: Mapped[float | None] = mapped_column(Numeric(6, 2))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # === PATCH_SMM_CATEGORY_LINK_PATTERN_V1 ===
    # نمونه‌لینکِ ثبت‌شده توسطِ ادمین برایِ این دسته (مثلاً یه لینکِ نمونهٔ
    # اینستاگرام). فقط دامنه‌اش موقعِ خرید چک می‌شود تا کاربر لینکِ
    # پلتفرمِ اشتباه نفرستد. NULL یعنی این دسته چک نمی‌شود (رفتارِ قبلی).
    link_pattern: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SmmService(Base):
    """یک سرویس SMM که ادمینِ ربات از پنل انتخاب کرده و برای فروش گذاشته است.

    قیمت‌گذاری دو حالته است (بر اساس تنظیمِ smm_price_mode ربات):
      • margin  → قیمت فروش = قیمت پنل × (۱ + درصد سود)  (sell_rate خالی می‌ماند)
      • manual  → قیمت فروش دستی در sell_rate ذخیره می‌شود (به‌ازای هر ۱۰۰۰).
    """
    __tablename__ = "smm_services"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    service_id: Mapped[str] = mapped_column(String(40))       # شناسهٔ سرویس در پنل
    name: Mapped[str] = mapped_column(String(255))
    category: Mapped[str | None] = mapped_column(String(255))
    panel_rate: Mapped[float] = mapped_column(Numeric(16, 4), default=0)  # قیمت پنل هر ۱۰۰۰ (واحد پنل)
    sell_rate: Mapped[float] = mapped_column(Numeric(16, 0), default=0)   # قیمت فروش دستی هر ۱۰۰۰ (تومان)
    min_qty: Mapped[int] = mapped_column(Integer, default=0)
    max_qty: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # سرویس دستی (به پنل وصل نیست؛ سفارش همیشه با تأیید ادمین انجام می‌شود)
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # === PATCH_SMM_SERVICE_DESCRIPTION_V1 ===
    # توضیحاتِ دلخواهِ ادمین برایِ این سرویس — به مشتری قبل از خرید نشان
    # داده می‌شود (مثلاً کیفیت، سرعتِ تحویل، ریزش/Drop، شرایط و غیره).
    description: Mapped[str | None] = mapped_column(Text)
    # === PATCH_SMM_PROVIDER_DESC_V1 ===
    # توضیحاتی که خودِ ارائه‌دهنده (پنل) برایِ این سرویس فرستاده — جدا از
    # `description` بالا که متنِ دستیِ ادمین است. موقعِ افزودنِ سرویس و هر
    # بار بروزرسانیِ لیستِ سرویس‌ها از پنل پر می‌شود. NULL یعنی پنل
    # توضیحاتی نداده یا سرویس دستی است.
    provider_desc: Mapped[str | None] = mapped_column(Text)
    # === PATCH_SMM_DESC_TRANSLATE_V1 === ترجمهٔ فارسی provider_desc و هش متن مبدأ (تا فقط با تغییر متن دوباره ترجمه شود)
    provider_desc_fa: Mapped[str | None] = mapped_column(Text)
    provider_desc_fa_hash: Mapped[str | None] = mapped_column(String(16))
    # === PATCH_SMM_MULTI_PANEL_V1 ===
    # این سرویس از کدام پنل گرفته شده؛ NULL یعنی از همان پنلِ پیش‌فرضِ
    # قدیمی (Setting.smm_api_url/smm_api_key) — سرویس‌های ساخته‌شده قبل
    # از این فیچر همیشه NULL می‌مانند و کار می‌کنند.
    panel_pk: Mapped[int | None] = mapped_column(ForeignKey("smm_panels.id"))
    # === PATCH_SMM_MANUAL_CATEGORY_V1 ===
    # دسته‌ای که ادمین دستی ساخته و این سرویس را زیرش گذاشته — جدا از
    # فیلدِ category (که فقط رشتهٔ خامِ برگشتی از پنله). اگر ست بشه و
    # خودِ دسته margin_pct داشته باشه، تویِ محاسبهٔ قیمت اولویت داره.
    category_pk: Mapped[int | None] = mapped_column(ForeignKey("smm_categories.id"))
    # === PATCH_SMM_STORED_CLASSIFICATION_V1 ===
    # پلتفرم/نوعی که موقعِ افزودن از پنل، ادمین دقیقاً همون رو مرور و
    # تأیید کرده (مثلاً پرمیوم/بوست/ممبرِکانال) — قبلاً این اطلاعات بعدِ
    # ثبت دور ریخته می‌شد و بعداً هرجا لازم بود (مثلاً «مدیریتِ محصولاتِ
    # من») دوباره از رویِ اسم حدس زده می‌شد، که می‌تونست با چیزی که ادمین
    # واقعاً انتخاب کرده بود فرق کنه. الان همون انتخابِ تأییدشده ذخیره
    # می‌شه؛ NULL یعنی سرویسِ قدیمی/دستی که این اطلاعات رو نداره (هنوز
    # با حدسِ نام fallback می‌شه، برای سازگاری).
    platform_key: Mapped[str | None] = mapped_column(String(30))
    type_key: Mapped[str | None] = mapped_column(String(30))
    # === PATCH_SMM_MANUAL_SORT_ORDER_V1 ===
    # برایِ حالتِ «ترتیبِ دستی» در smm_sort_order (references/smm-menu-redesign.md
    # در اسکیلِ botsaz-dev، تصمیمِ ۵): عددِ کوچیک‌تر بالاترِ لیست می‌آید.
    # پیش‌فرض ۰ برایِ همه — یعنی تا وقتی ادمین با دکمه‌هایِ 🔼/🔽 جابه‌جا
    # نکرده، ترتیب اثری ندارد (هم‌ارز با id).
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # === PATCH_SMM_INPUT_KIND_V1 === آنچه مشتری هنگامِ سفارش می‌فرستد: ""/"link" = لینک (پیش‌فرض)،
    # "number" = فقط عدد (آیدی/شماره، مثلاً «میو پوینت»)، "text" = متنِ آزاد
    input_kind: Mapped[str | None] = mapped_column(String(10), default="", server_default="")


class SmmOrder(Base):
    """یک سفارش SMM ثبت‌شده توسط کاربر (افزایش ممبر/ویو/... از طریق پنل)."""
    __tablename__ = "smm_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    smm_service_pk: Mapped[int | None] = mapped_column(Integer)   # id ردیف SmmService
    service_id: Mapped[str] = mapped_column(String(40))           # شناسهٔ سرویس در پنل
    service_name: Mapped[str | None] = mapped_column(String(255))
    link: Mapped[str] = mapped_column(Text)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    price_toman: Mapped[float] = mapped_column(Numeric(14, 0), default=0)  # مبلغ کسرشده از کیف پول
    # وضعیت داخلی ربات:
    #   awaiting_admin = منتظر تأیید ادمین (حالت دستی)
    #   sent           = روی پنل ثبت شد، منتظر تکمیل
    #   completed      = پنل گزارش Completed داد
    #   partial        = نیمه‌تمام: بخشی تحویل شد و مابقی برگشت (PATCH_SMM_PARTIAL_V1)
    #   failed / canceled / refunded
    status: Mapped[str] = mapped_column(String(20), default="awaiting_admin", index=True)
    panel_order_id: Mapped[str | None] = mapped_column(String(40))  # شناسهٔ سفارش در پنل
    panel_status: Mapped[str | None] = mapped_column(String(30))    # آخرین status پنل
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # === PATCH_ORDER_TRACKING_V1 ===
    tracking_code: Mapped[str | None] = mapped_column(String(20), index=True)
    # === PATCH_VS_DISCOUNT_V1 === کدِ تخفیفِ مصرف‌شده (برایِ برگرداندنِ مصرف هنگامِ رد/عودت). NULL = بدونِ کد
    discount_code_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discount_amount: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # === PATCH_VS_ORDER_DETAIL_V1 ===
    # برای صفحهٔ جزئیاتِ سفارش (مشاهده سفارشات → لیست/جستجو): مقدارِ
    # remains که پنل موقعِ استعلامِ وضعیت برمی‌گرداند (چقدر از تعداد هنوز
    # تحویل داده نشده)، هزینهٔ خامِ پنل قبل از اضافه‌شدنِ سود (برای اینکه
    # بشود «سود = قیمتِ سفارش − هزینه» را نشان داد؛ فقط برای سفارش‌های
    # متصل به پنل با فرمولِ خودکار پر می‌شود، برای بقیه None می‌ماند)، و
    # آخرین‌باری که وضعیتِ این سفارش تغییر کرده.
    remaining: Mapped[int | None] = mapped_column(Integer)
    cost_toman: Mapped[float | None] = mapped_column(Numeric(14, 0))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    # === PATCH_SMM_MULTI_PANEL_V1 ===
    # کپیِ panel_pkِ سرویس در لحظهٔ ثبتِ سفارش — چون بعداً ممکن است خودِ
    # سرویس حذف/غیرفعال شود ولی سفارش هنوز باید بداند کدام پنل را استعلام
    # کند. NULL یعنی پنلِ پیش‌فرضِ قدیمی.
    panel_pk: Mapped[int | None] = mapped_column(ForeignKey("smm_panels.id"))
    # === PATCH_SMM_PARTIAL_V1 ===
    # مبلغی که در سفارشِ «نیمه‌تمام» (Partial) به کیف‌پولِ کاربر برگشت (تومان). برایِ سفارش‌هایِ
    # عادی NULL است. با آن «مبلغ نهایی سفارش = پرداختی − بازگشتی» حساب می‌شود.
    refunded_toman: Mapped[float | None] = mapped_column(Numeric(14, 0))
    # === PATCH_API_PAGE_CLEAN_V1 === مبدأ ثبتِ سفارش: "api" = از راهِ API (v1/v2)؛ NULL = از خودِ ربات.
    # برایِ لیستِ «سفارش‌هایِ API» و شمارندهٔ صفحهٔ API کاربر.
    source: Mapped[str | None] = mapped_column(String(12))


# === PATCH_PLATFORM_GROUP_GUARD_V1 ===
# «محافظِ گروه» — قابلیتِ مدیریتِ گروه/کانالِ خودِ پلتفرم (بات‌ساز)، نه
# ربات‌های مستأجر. عمداً به bots.id ربطی ندارد (به همین دلیل عضوِ
# TENANT_OWNED_MODELS نیست) — دیتای سطحِ پلتفرم است، فقط با chat_id
# (آیدیِ عددیِ گروه/کانالِ متصل‌شده) مرتبط می‌شود.
class GroupGuardBan(Base):
    """کاربرانِ بن‌شده در گروهِ محافظت‌شده."""
    __tablename__ = "group_guard_bans"
    __table_args__ = (UniqueConstraint("chat_id", "user_id", name="uq_ggban_chat_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    full_name: Mapped[str | None] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(String(255))
    banned_by: Mapped[int | None] = mapped_column(BigInteger)
    banned_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class GroupGuardWarn(Base):
    """شمارندهٔ خطا (اخطار) هر کاربر در گروهِ محافظت‌شده."""
    __tablename__ = "group_guard_warns"
    __table_args__ = (UniqueConstraint("chat_id", "user_id", name="uq_ggwarn_chat_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    full_name: Mapped[str | None] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(64))
    count: Mapped[int] = mapped_column(Integer, default=0)
    last_reason: Mapped[str | None] = mapped_column(String(255))
    last_warned_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now())


class GroupGuardCaptcha(Base):
    """کپچایِ در حالِ انتظار برایِ اعضایِ تازه‌واردِ گروهِ محافظت‌شده."""
    __tablename__ = "group_guard_captchas"
    __table_args__ = (UniqueConstraint("chat_id", "user_id", name="uq_ggcaptcha_chat_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger)
    full_name: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


# === PATCH_SELF_SERVICE_TOKEN_OWNER_V1 ===
class SelfServiceBotChange(Base):
    """لاگِ درخواست‌هایِ خودکارِ (بدونِ نیاز به تاییدِ دستیِ ادمین) تعویضِ
    توکن یا واگذاریِ مالکیتِ ربات توسطِ خودِ کاربر — فقط برایِ اعمالِ
    محدودیتِ ماهانه (۲ بار در ماه برایِ کاربرانِ عادی؛ مدیرِ اصلیِ پلتفرم
    نامحدود است و اصلاً اینجا شمرده نمی‌شود)."""
    __tablename__ = "self_service_bot_changes"

    id: Mapped[int] = mapped_column(primary_key=True)
    requested_by: Mapped[int] = mapped_column(BigInteger, index=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"))
    kind: Mapped[str] = mapped_column(String(20))  # "token" | "owner"
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# === PATCH_DICE_GAME_V1 ===
class DiceGame(Base):
    """یک «بازی کن پول بگیر» ثبت‌شده برای یک ربات.

    بازی‌ها از بازی‌های داخلیِ خودِ تلگرام استفاده می‌کنند (send_dice) — یعنی
    نتیجه را سرورِ تلگرام تعیین می‌کند و ربات نمی‌تواند در آن دست ببرد.
    prizes یک JSON از «مقدارِ نتیجه → جایزه به تومان» است، مثلاً
    {"1": 1000, "2": 1000, ..., "6": 5000}.
    """
    __tablename__ = "dice_games"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    emoji: Mapped[str] = mapped_column(String(8), default="🎲")
    title: Mapped[str] = mapped_column(String(80), default="تاس")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    prizes: Mapped[str | None] = mapped_column(Text)      # JSON: {"1": 1000, ...}
    # === PATCH_DICE_GAME_PLAN_PRIZE_V1 ===
    # JSON اختیاری: «مقدارِ نتیجه → آیدیِ VpnPlan». اگر نتیجه‌ای اینجا باشد،
    # علاوه بر جایزهٔ نقدی، یک سرویسِ VPنِ رایگان از همان پلن برای کاربر
    # ساخته می‌شود. خالی/None یعنی این بازی هیچ جایزهٔ VPN‌ای ندارد.
    plan_prizes: Mapped[str | None] = mapped_column(Text)
    tries_limit: Mapped[int] = mapped_column(Integer, default=1)   # 0 = نامحدود
    tries_period: Mapped[str] = mapped_column(String(10), default="day")  # day/week/total
    # اجازهٔ بازی در گروه‌ها/سوپرگروه‌ها (نه فقط چتِ خصوصی)
    in_groups: Mapped[bool] = mapped_column(Boolean, default=True)
    # اگر پر باشد، بازی فقط در همین گروه کار می‌کند (اتصالِ اختصاصی به یک گروه)
    group_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    group_title: Mapped[str | None] = mapped_column(String(120))
    bind_code: Mapped[str | None] = mapped_column(String(12), index=True)
    # === PATCH_DICE_WINNER_NOTIFY_V1 ===
    # اگر روشن باشد، هر بار یک کاربر در این بازی برنده شود، یک پیامِ حرفه‌ایِ
    # «برندهٔ جدید» با مشخصاتِ کاربر و جایزه برای مالکِ ربات فرستاده می‌شود.
    # پیش‌فرض خاموش تا رفتارِ فعلیِ ربات‌ها تغییر نکند و اسپم نشود.
    notify_owner_on_win: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DicePlay(Base):
    """هر بار بازی‌کردنِ یک کاربر (برای شمارشِ سهمیه و گزارش‌گیری)."""
    __tablename__ = "dice_plays"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("dice_games.id"), index=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    value: Mapped[int] = mapped_column(Integer, default=0)   # نتیجهٔ تاس
    amount: Mapped[int] = mapped_column(Integer, default=0)  # جایزهٔ پرداخت‌شده (تومان)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PlatformTonPaysCharge(Base):
    """شارژ کیف پولِ بات‌ساز از طریق درگاه TonPays (کارت‌به‌کارتِ خودکار).

    مثلِ نسخهٔ مستأجر، بدنهٔ کال‌بک هرگز ملاکِ شارژ نیست؛ همیشه با استعلامِ
    مستقلِ /invoices/check از خودِ درگاه تایید گرفته می‌شود.
    """
    __tablename__ = "platform_tonpays_charges"
    __table_args__ = (UniqueConstraint("order_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    order_id: Mapped[str] = mapped_column(String(20), index=True)
    invoice_id: Mapped[str | None] = mapped_column(String(64), index=True)
    request_amount: Mapped[int] = mapped_column(Integer, default=0)
    final_amount: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="waiting")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PlatformTonDirectCharge(Base):
    """شارژ کیف پولِ بات‌ساز با واریزِ مستقیمِ TON به ولتِ خودِ بات‌ساز.

    === PATCH_TON_DIRECT_PLATFORM_V1 ===
    نسخهٔ پلتفرمیِ TonDirectCharge (tenant). چون این‌جا فقط یک ولتِ سراسری
    (متعلق به خودِ بات‌ساز) وجود دارد، برخلافِ نسخهٔ مستأجر ستونِ bot_id
    ندارد. تشخیصِ پرداخت هم مثلِ نسخهٔ مستأجر دو‌لایه است: اول memo، بعد
    مبلغِ یکتا. tx_hash یکتاست تا یک تراکنش هرگز دوبار شارژ نشود.
    """
    __tablename__ = "platform_ton_direct_charges"
    __table_args__ = (UniqueConstraint("memo", name="uq_platform_tondirect_memo"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    memo: Mapped[str] = mapped_column(String(24), index=True)
    amount_toman: Mapped[int] = mapped_column(Numeric(14, 0), default=0)
    ton_amount: Mapped[float] = mapped_column(Numeric(18, 9), default=0)
    rate_toman: Mapped[int] = mapped_column(Numeric(14, 0), default=0)
    wallet_address: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default="waiting")  # waiting/paid/expired
    tx_hash: Mapped[str | None] = mapped_column(String(80), index=True)
    invoice_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    invoice_message_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)


class PlatformApayCharge(Base):
    """=== PATCH_PLATFORM_APAY_V1 === شارژِ کیف پولِ بات‌ساز با درگاهِ آپی (apaygate.shop).
    مبلغ‌ها: amount_toman آنچه شارژ می‌شود؛ pay_amount_rial مبلغِ دقیقِ یکتایِ واریز."""
    __tablename__ = "platform_apay_charges"
    __table_args__ = (UniqueConstraint("order_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    order_id: Mapped[str] = mapped_column(String(40), index=True)
    payment_id: Mapped[str | None] = mapped_column(String(64), index=True)
    amount_toman: Mapped[int] = mapped_column(Integer, default=0)
    fee_toman: Mapped[int] = mapped_column(Integer, default=0, server_default="0")   # PATCH_APAY_FEE_PAYER_V1
    pay_amount_rial: Mapped[int | None] = mapped_column(BigInteger)
    pay_url: Mapped[str | None] = mapped_column(String(300))
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    msg_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), default="waiting")  # waiting/paid/cancelled/expired
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    admin_asked_at: Mapped[datetime | None] = mapped_column(DateTime)   # PATCH_APAY_ADMIN_ASK_V1
    admin_ask_chat: Mapped[int | None] = mapped_column(BigInteger)       # PATCH_APAY_ASK_CLEANUP_V1
    admin_ask_msg: Mapped[int | None] = mapped_column(BigInteger)
    # === PATCH_PARTNER_APAY_V1 === «wallet» = شارژِ کیف پول | «partner_order» = خریدِ عمدهٔ همکار ( ref_id = PartnerStockOrder.id)
    purpose: Mapped[str] = mapped_column(String(20), default="wallet", server_default="wallet")
    ref_id: Mapped[int | None] = mapped_column(Integer)


class PlatformBluCharge(Base):
    """شارژ کیف پولِ بات‌ساز از طریق درگاه بلوپال (blupal.net).

    نکته‌ها:
      • APIِ بلو مبلغ را به **ریال** می‌گیرد، ولی کیف پولِ بات‌ساز به تومان
        است؛ پس هر دو نگه داشته می‌شود تا هیچ‌جا ضرب/تقسیمِ حدسی لازم نشود.
      • بلو فیلدِ order_id ندارد، پس کلیدِ تطبیق همان invoice_id عددیِ خودشان است.
      • وبهوکِ بلو امضا ندارد و ممکن است چند بار ارسال شود؛ برایِ همین بدنهٔ
        وبهوک هرگز ملاکِ شارژ نیست و همیشه با GET /invoices/{id} تایید می‌گیریم.
    """
    __tablename__ = "platform_blu_charges"
    __table_args__ = (UniqueConstraint("invoice_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    invoice_id: Mapped[int] = mapped_column(BigInteger, index=True)
    amount_toman: Mapped[int] = mapped_column(Integer, default=0)   # آنچه شارژ می‌شود
    amount_rial: Mapped[int] = mapped_column(Integer, default=0)    # آنچه به بلو دادیم
    final_amount_rial: Mapped[int] = mapped_column(Integer, default=0)  # مبلغِ یکتایِ واریز
    card_number: Mapped[str | None] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(10), default="live")   # live / sandbox
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PlatformDiceGame(Base):
    """«بازی کن پول بگیر» روی خودِ رباتِ بات‌ساز (نه ربات‌های مستأجر)."""
    __tablename__ = "platform_dice_games"

    id: Mapped[int] = mapped_column(primary_key=True)
    emoji: Mapped[str] = mapped_column(String(8), default="🎲")
    title: Mapped[str] = mapped_column(String(80), default="تاس")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    prizes: Mapped[str | None] = mapped_column(Text)      # JSON: {"1": 1000, ...}
    tries_limit: Mapped[int] = mapped_column(Integer, default=1)   # 0 = نامحدود
    tries_period: Mapped[str] = mapped_column(String(10), default="day")
    in_groups: Mapped[bool] = mapped_column(Boolean, default=True)
    group_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    group_title: Mapped[str | None] = mapped_column(String(120))
    bind_code: Mapped[str | None] = mapped_column(String(12), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PlatformDicePlay(Base):
    __tablename__ = "platform_dice_plays"

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("platform_dice_games.id"), index=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    value: Mapped[int] = mapped_column(Integer, default=0)
    amount: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# === PATCH_FAKE_JOIN_GUARD_V1 ===
class FakeJoinCandidate(Base):
    """اعضایِ مشکوک به فیک، شناسایی‌شده با burst-join detector (شناساییِ
    عضویتِ انبوه و پشتِ‌سرِهم — مثلاً بالایِ ۲۰۰ نفر با فاصله‌ی ۱ تا ۱۰
    ثانیه). تا وقتی ادمین دکمه‌ی حذف رو نزنه یا نادیده بگیره، اینجا
    می‌مونن."""
    __tablename__ = "fake_join_candidates"
    __table_args__ = (UniqueConstraint("chat_id", "user_id", name="uq_fjg_chat_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    full_name: Mapped[str | None] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(64))
    detected_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# === PATCH_SELF_SERVICE_TOKEN_OWNER_V1 ===


# ======================= ربات همکار =======================
# === PATCH_PARTNER_BOT_V1 ===

class PartnerRequest(Base):
    """درخواستِ همکاری که کاربرِ بات‌ساز ثبت می‌کند تا «ربات همکار» بگیرد."""
    __tablename__ = "partner_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    full_name: Mapped[str | None] = mapped_column(String(120))
    username: Mapped[str | None] = mapped_column(String(64))
    # متنِ معرفیِ خودِ متقاضی (چرا می‌خواهد همکار شود، سابقه، حجمِ فروش و...)
    note: Mapped[str | None] = mapped_column(Text)
    # pending / approved / rejected / canceled
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # دلیلِ رد (فقط وقتی status="rejected") — عیناً به متقاضی نشان داده می‌شود
    reject_reason: Mapped[str | None] = mapped_column(Text)
    # === PATCH_PARTNER_CREATE_V1 ===
    # تعدادِ روزی که مدیر هنگامِ تایید تعیین کرده؛ موقعِ ساختِ رباتِ همکار
    # به expires_at تبدیل می‌شود. null = هنوز تایید نشده.
    granted_days: Mapped[int | None] = mapped_column(Integer)
    # آیا متقاضی قوانینِ ضدکلاهبرداری را پذیرفته است
    terms_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # === PATCH_PARTNER_FINALIZE_LIST_V1 ===
    # چت/پیامِ اطلاع‌رسانیِ اولیه به مدیر، تا هنگامِ تایید/رد بتوان
    # همان پیام را ویرایش کرد و دکمه‌هایش را حذف نمود.
    admin_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    admin_msg_id: Mapped[int | None] = mapped_column(BigInteger)


class PartnerMessage(Base):
    """گفت‌وگویِ دوطرفهٔ مدیرِ پلتفرم با متقاضی/همکار — شبیهِ تیکت."""
    __tablename__ = "partner_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("partner_requests.id"), index=True)
    # "admin" یعنی از طرفِ مدیرِ پلتفرم، "user" یعنی از طرفِ متقاضی
    sender: Mapped[str] = mapped_column(String(10), default="admin")
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PartnerEscalation(Base):
    """=== PATCH_PARTNER_V12 ===
    تیکتِ ارجاعِ همکار به پشتیبانیِ بات‌ساز — گفت‌وگویِ دوطرفه، جدا از
    تیکت‌هایِ عادیِ کاربران تا در پنلِ مادر قاطی نشوند."""
    __tablename__ = "partner_escalations"

    id: Mapped[int] = mapped_column(primary_key=True)
    partner_bot_id: Mapped[int] = mapped_column(
        ForeignKey("partner_bots.id"), index=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    subject: Mapped[str | None] = mapped_column(String(200))
    # open / answered / closed
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PartnerEscalationMsg(Base):
    """پیام‌هایِ یک تیکتِ ارجاع."""
    __tablename__ = "partner_escalation_msgs"

    id: Mapped[int] = mapped_column(primary_key=True)
    escalation_id: Mapped[int] = mapped_column(
        ForeignKey("partner_escalations.id"), index=True)
    # "partner" یعنی از طرفِ صاحبِ رباتِ همکار، "admin" یعنی از طرفِ مدیرِ پلتفرم
    sender: Mapped[str] = mapped_column(String(10), default="partner")
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PartnerBot(Base):
    """ربات همکارِ فعال‌شده (یک‌به‌یک با یک ردیفِ Bot که plan="partner" دارد).

    ربات‌هایی که این همکار می‌فروشد، ربات‌های مستأجرِ عادیِ همین پلتفرم‌اند
    (plan=growth/pro) و از طریقِ PartnerSale به این ردیف وصل می‌شوند.
    """
    __tablename__ = "partner_bots"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), unique=True, index=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    request_id: Mapped[int | None] = mapped_column(ForeignKey("partner_requests.id"))

    # درصدِ تخفیفِ همکار روی قیمتِ عادیِ پلن‌ها (پیش‌فرض ۳۰). مدیرِ پلتفرم
    # می‌تواند برای هر همکار جداگانه کم/زیادش کند.
    discount_percent: Mapped[int] = mapped_column(Integer, default=30, server_default="30")

    # موجودیِ ربات‌های خریداری‌شدهٔ آمادهٔ فروش (کاهش می‌یابد با هر فروش)
    stock_growth: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    stock_pro: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # جمعِ کلِ خریداری‌شده از ابتدا (فقط برای آمار — هرگز کم نمی‌شود)
    bought_growth: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    bought_pro: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # active / paused / expired — paused یعنی مدیرِ پلتفرم موقتاً متوقفش کرده
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    # تاریخِ انقضایِ خودِ ربات همکار (مدیرِ پلتفرم با +/- روز تنظیمش می‌کند)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)

    # === قانونِ «کمتر از ۱۰ ربات فعال → مهلتِ LOW_STOCK_GRACE_DAYS روزه» ===
    # تایمرِ کشویی: هر لحظه که تعدادِ ربات‌های فعالِ فروخته‌شده زیرِ حدِ نصاب
    # برود این تاریخ ست می‌شود؛ به‌محضِ رسیدن دوباره به حدِ نصاب null می‌شود.
    low_stock_since: Mapped[datetime | None] = mapped_column(DateTime)
    # آخرین باری که اخطارِ این مهلت به همکار اطلاع داده شد (YYYY-MM-DD)
    low_stock_notice: Mapped[str | None] = mapped_column(String(10))

    # آیا دکمهٔ «درخواست تست ۲ روزه» در ربات همکار فعال است
    trial_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# === PATCH_RESELLER_TRIAL_LOG_V1 ===
class ResellerTrialLog(Base):
    """هر تستی که نماینده از پنلِ وب ساخته.

    چرا جدولِ جدا: سهمیه قبلاً از رویِ خودِ کانفیگ‌هایِ موجود شمرده می‌شد،
    پس نماینده کافی بود تستش را از «سرویس‌های من» پاک کند تا سهمیه‌اش
    برگردد. این جدول فقط رشد می‌کند و با حذفِ کانفیگ پاک نمی‌شود.
    """
    __tablename__ = "reseller_trial_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    panel_key: Mapped[str | None] = mapped_column(String(30))
    account_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 index=True)


# === PATCH_PARTNER_STOCK_LOG_V1 ===
class PartnerStockLog(Base):
    """دفترچهٔ حرکاتِ انبارِ رباتِ همکار.

    چرا لازم شد: همکار فقط عددِ نهایی («رشد ۲ | حرفه‌ای ۰») را می‌دید و
    وقتی عدد پایین می‌آمد نمی‌فهمید چرا — مثلاً «ارتقا از رشد به حرفه‌ای»
    یک واحدِ حرفه‌ای مصرف می‌کند بی‌آنکه فروشِ جدیدی ثبت شود. این جدول هر
    کم‌وزیادِ انبار را با دلیلش نگه می‌دارد.
    """
    __tablename__ = "partner_stock_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    partner_bot_id: Mapped[int] = mapped_column(ForeignKey("partner_bots.id"), index=True)
    plan: Mapped[str] = mapped_column(String(10))          # growth | pro
    delta: Mapped[int] = mapped_column(Integer)            # +2 خرید، -1 فروش
    reason: Mapped[str] = mapped_column(String(40))        # buy|sale|upgrade|admin|refund
    note: Mapped[str | None] = mapped_column(String(160))
    balance_after: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PartnerSale(Base):
    """یک ربات که همکار به کاربرِ خودش فروخته/تحویل داده است."""
    __tablename__ = "partner_sales"

    id: Mapped[int] = mapped_column(primary_key=True)
    partner_bot_id: Mapped[int] = mapped_column(ForeignKey("partner_bots.id"), index=True)
    # رباتِ مستأجرِ ساخته‌شده (plan=growth/pro)
    sold_bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    buyer_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    plan: Mapped[str] = mapped_column(String(20))  # growth / pro
    # مبلغی که همکار از خریدار گرفته (برای گزارشِ تسویه). صفر = تست/هدیه
    price: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    # sold / trial — ربات‌های تستِ ۲ روزه در حدِ نصابِ ۱۰ تایی شمرده نمی‌شوند
    kind: Mapped[str] = mapped_column(String(10), default="sold")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PartnerCardOrder(Base):
    """=== PATCH_PARTNER_V20 ===
    سفارشِ خریدِ رباتِ کاربر از همکار، با کارت‌به‌کارت.

    چرا جدولِ جدا از PartnerSale: تا لحظهٔ تاییدِ رسید توسطِ همکار، هنوز
    هیچ رباتی ساخته نشده و هیچ‌چیزی از انبار کم نشده. PartnerSale فقط
    وقتی ساخته می‌شود که ربات واقعاً تحویل داده شود."""
    __tablename__ = "partner_card_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    partner_bot_id: Mapped[int] = mapped_column(
        ForeignKey("partner_bots.id"), index=True)
    buyer_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    plan: Mapped[str] = mapped_column(String(20))          # growth / pro
    price: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    receipt_file_id: Mapped[str | None] = mapped_column(String(200))
    # pending / approved / rejected
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # رباتی که بعد از تایید ساخته شد (برایِ پیگیری)
    sold_bot_id: Mapped[int | None] = mapped_column(Integer)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PartnerStockOrder(Base):
    """سفارشِ خریدِ عمدهٔ ربات توسطِ همکار (کارت‌به‌کارت یا کیف پول)."""
    __tablename__ = "partner_stock_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    partner_bot_id: Mapped[int] = mapped_column(ForeignKey("partner_bots.id"), index=True)
    qty_growth: Mapped[int] = mapped_column(Integer, default=0)
    qty_pro: Mapped[int] = mapped_column(Integer, default=0)
    # مبلغِ نهایی پس از اعمالِ discount_percent
    amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    # card / wallet
    method: Mapped[str] = mapped_column(String(10), default="card")
    # pending / approved / rejected
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    receipt_file_id: Mapped[str | None] = mapped_column(String(200))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EscrowDeal(Base):
    """=== PATCH_ESCROW_V1 === معاملهٔ با واسط (خدماتِ مجازی): خریدار/فروشنده، مبلغ، نرخِ واسطه، گروهِ ساخته‌شده."""
    __tablename__ = "escrow_deals"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    number: Mapped[int] = mapped_column(Integer, index=True)            # شمارهٔ معامله در همین ربات
    initiator_tg: Mapped[int] = mapped_column(BigInteger, index=True)
    counterparty_tg: Mapped[int] = mapped_column(BigInteger, index=True)
    initiator_role: Mapped[str] = mapped_column(String(10))             # seller / buyer
    title: Mapped[str] = mapped_column(String(80))
    amount_toman: Mapped[int] = mapped_column(BigInteger, default=0)
    fee_percent: Mapped[float] = mapped_column(Float, default=0)
    fee_toman: Mapped[int] = mapped_column(BigInteger, default=0)
    fee_payer: Mapped[str] = mapped_column(String(10), default="buyer")  # buyer / seller / split
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    group_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    group_title: Mapped[str | None] = mapped_column(String(160))
    invite_buyer: Mapped[str | None] = mapped_column(String(200))
    invite_seller: Mapped[str | None] = mapped_column(String(200))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # تأییدِ هر طرف در گروه (دکمهٔ «اوکی»)؛ پایان/لغو فقط با واسط
    buyer_ok: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    seller_ok: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class RefPrizeClaim(Base):
    """جایزهٔ امتیازِ رفرال که کاربر باید انتخابش کند یا ادمین دستی ارسالش کند — PATCH_REFPOINT_PRIZES_V1.
    status: choose (کاربر هنوز انتخاب نکرده) / pending (منتظرِ ارسالِ دستیِ ادمین) / done / failed."""
    __tablename__ = "ref_prize_claims"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    label: Mapped[str | None] = mapped_column(String(160))
    kind: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(10), default="choose", index=True)
    note: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)


class BalanceTransfer(Base):
    """انتقالِ موجودی بینِ دو کاربرِ یک ربات — PATCH_BALANCE_TRANSFER_V1.
    status: pending (منتظرِ تأییدِ ادمین؛ مبلغ از فرستنده کسر و نگه داشته شده) / done / rejected."""
    __tablename__ = "balance_transfers"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    from_user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    to_user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    amount: Mapped[float] = mapped_column(Numeric(12, 0))
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)


# هنگام حذف کامل یک ربات (پاک‌سازی بایگانی) داده‌های همهٔ این جدول‌ها حذف می‌شوند.
# === PATCH_BOTUSER_DELETE_ORDER_V1 ===
# BotUser باید آخرِ این لیست باشد، نه اول — چون تقریباً همهٔ جداولِ دیگرِ
# این لیست (WalletTxn.user_id، Config.owner_user_id، Order.user_id،
# TenantTicket.user_id، ProductOrder.user_id، Appointment.user_id،
# ReferralLink.user_id، ReferralJoin.inviter_user_id، BotReview.user_id،
# GatedClaim.user_id) یک ForeignKey("bot_users.id") دارند. وقتی BotUser
# اول لیست بود، purge_bot سعی می‌کرد قبل از پاک‌کردنِ این جداولِ وابسته،
# خودِ BotUser را حذف کند و به خطای foreign key می‌خورد (دقیقاً همان
# خطایی که موقعِ حذفِ بات‌ها دیده می‌شد: «خطا در حذف جدول BotUser: ...
# violates foreign key constraint»).
TENANT_OWNED_MODELS = [
    WalletTxn, Config, Order, ButtonSetting, Setting, DiscountCode,
    TenantTicket, Broadcast, ForceJoin, AdminSupport, MainButton, Product,
    ProductOrder, Appointment, AppointmentSlot, Poll, ReferralLink, ReferralJoin, ReferralOrderReward,
    BotReview, AutoMessage, BotMenuItem, VpnPanel, VpnPlan, PanelAccount,
    StockRequest, CryptoCharge, GatedClaim, ProductRating,  # PATCH_PRODUCT_RATING_V1
    TrialUsage, ButtonTrialClaim, DiscountUsage, SmmService,
    SmmRefill,   # === PATCH_SMM_REFILL_V1 === FK به smm_orders ⇒ باید *قبل* از SmmOrder پاک شود
    SmmOrder,
    # === PATCH_GATE_DEDICATED_LINK_V1 ===
    GateReferralLink,
    EscrowDeal,   # === PATCH_ESCROW_V1 ===
    BalanceTransfer,   # === PATCH_BALANCE_TRANSFER_V1 ===
    MeteredAccount,    # === PATCH_METERED_PLAN_V1 ===
    StockDelivery,     # === PATCH_STOCK_MANUAL_DELIVERY_V1 ===
    RefPrizeClaim,   # === PATCH_REFPOINT_PRIZES_V1 ===
    BotUser,
    # === PATCH_TENANT_OWNED_MODELS_MISSING_TABLES_V1 ===
    # این ۴ تا جدول (که همه‌شون تازه این فصل اضافه شدن: پنلِ تبلیغِ
    # زمان‌بندی‌شده، پست‌های یک‌بارهٔ زمان‌بندی‌شده، و علاقه‌مندی‌های
    # اسلایدشو) هیچ‌وقت به این لیست اضافه نشده بودن — یعنی «تعویضِ توکنِ
    # ربات» دیتاشون رو منتقل نمی‌کرد، و اگه رباتِ قدیم حتی یه ردیف تو
    # یکی از این جدول‌ها داشت (مثلاً همین که یه‌بار پنلِ تبلیغ رو باز
    # کرده بود کافی بود)، حذفِ رباتِ قدیم در پایانِ انتقال با خطای
    # «foreign key constraint» شکست می‌خورد.
    AdBroadcast, AdBroadcastTarget, AdScheduledPost, SlideFavorite,
    # === PATCH_FILE_LINK_CONVERTER_V1 ===
    FileLink,
    TonPaysCharge,  # === PATCH_TONPAYS_V1 ===
    # === PATCH_DICE_GAME_V1 === (DicePlay فرزندِ DiceGame است، پس اول بیاید)
    DicePlay, DiceGame,
    # === PATCH_TENANT_OWNED_MODELS_MISSING_TABLES_V2 ===
    # این دوتا هم مثلِ همون ۴ تایِ بالا هیچ‌وقت به لیست اضافه نشده بودن.
    # SelfServiceBotChange و MiniAppCategory هر دو bot_id مستقیم به bots.id
    # دارن؛ نبودشون تو این لیست باعثِ خطایِ «foreign key constraint»
    # (self_service_bot_changes_bot_id_fkey / miniapp_categories_bot_id_fkey)
    # موقعِ حذفِ نهاییِ ربات می‌شد — دقیقاً همون خطایی که تو لاگ افتاده بود.
    SelfServiceBotChange, MiniAppCategory,
    # VnumNumber هم به bots.id و هم به smm_services.id اشاره می‌کند؛ نبودش
    # تو این لیست باعثِ «vnum_numbers_country_service_pk_fkey» می‌شد وقتی
    # SmmService حذف می‌شد ولی شماره‌هایِ وابسته‌اش هنوز باقی بودن.
    VnumNumber,
    # === PATCH_FRAGMENT_WALLET_ARCHIVE_V1 ===
    # bot_id مستقیم به bots.id دارد ولی در این لیست نبود: purge_bot آن را فقط از مسیرِ
    # «جاروی» ته‌ِ تابع (SQLِ خام) پاک می‌کرد و migrate_bot_data دستش نمی‌زد. حالا رسماً
    # عضوِ لیست است؛ و purge_bot قبل از حذف، نسخهٔ بایگانی (FragmentWalletArchive) می‌سازد.
    FragmentWallet,
    # PartnerBot/PartnerSale عمداً اینجا نیستند: بلوکِ
    # PATCH_PURGE_PARTNER_LINKS_V1 در config.py آن‌ها را جداگانه و با
    # حفظِ سوابقِ مالی (NULL کردنِ ارجاع، نه حذف) مدیریت می‌کند.
]

# ButtonSlide/PollVote/TenantTicketReply به bots.id اشاره نمی‌کنند بلکه
# فرزندِ یه جدولِ دیگه‌ن (از طریقِ button_id/poll_id/ticket_id) — باید قبل
# از والدشان حذف بشن تا FK گیر نکنه.
# === PATCH_MISSING_CHILD_TABLES_V1 ===
# قبلاً PollVote و TenantTicketReply اصلاً توی هیچ‌کدوم از این سه لیست
# نبودن (نه TENANT_OWNED_MODELS چون bot_id مستقیم ندارن، نه اینجا) —
# یعنی purge_bot اصلاً این دو جدول رو پاک نمی‌کرد و اگه ردیفی توشون بود،
# حذفِ Poll/TenantTicket با خطای foreign key مواجه می‌شد.
TENANT_CHILD_VIA_MODELS = [
    (ButtonSlide, "button_id", MainButton, "bot_id"),
    (PollVote, "poll_id", Poll, "bot_id"),
    (TenantTicketReply, "ticket_id", TenantTicket, "bot_id"),
    # === PATCH_GATE_DEDICATED_LINK_V1 ===
    (GateReferralJoin, "link_id", GateReferralLink, "bot_id"),
    # === PATCH_PURGE_COMMUNITY_CHILDREN_V1 ===
    # لاگِ سرور: حذفِ نهاییِ رباتِ 99/315/740 هر چرخه با ForeignKeyViolation شکست می‌خورد
    # (community_filter_words به community_chats اشاره می‌کند ولی در هیچ فهرستِ حذف نبود).
    (CommunityWarn, "chat_row_id", CommunityChat, "bot_id"),
    (CommunityFilterWord, "chat_row_id", CommunityChat, "bot_id"),
]

# جداولی که به bot_id اشاره نمی‌کنند بلکه old_bot_id/new_bot_id دارند
# (درخواست تعویض توکن و انتقال مالکیت) — باید جدا در purge_bot پاک شوند.
TENANT_DUAL_REF_MODELS = [
    (TokenSwapRequest, "old_bot_id", "new_bot_id"),
    (OwnerTransferRequest, "old_bot_id", "new_bot_id"),
]

# PATCH_ADD_SHEBA_ACCOUNT_ID_TYPE


# ═══════════════════════════════════════════════════════════════════
# === PATCH_HOSTING_V1 === «سرور فعال‌سازی هوشمند ربات»
# کاربرِ بات‌ساز زیپِ کدِ رباتِ خودش را آپلود می‌کند و ما آن را داخلِ یک
# کانتینرِ ایزوله اجرا می‌کنیم. هر رکورد = یک اپلیکیشنِ کاربر.
# ═══════════════════════════════════════════════════════════════════

class HostedApp(Base):
    """اپلیکیشنِ میزبانی‌شدهٔ یک کاربر (زیپ → کانتینر)."""
    __tablename__ = "hosted_apps"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)  # telegram_id

    # نامِ یکتا در کلِ پلتفرم — همان چیزی که کاربر انتخاب می‌کند.
    # فقط a-z 0-9 و خط تیره؛ نامِ کانتینر و پوشه از همین ساخته می‌شود.
    name: Mapped[str] = mapped_column(String(40), unique=True, index=True)

    # python | node | dockerfile — از رویِ محتوایِ زیپ تشخیص داده می‌شود
    runtime: Mapped[str] = mapped_column(String(20), default="python")
    # فایلی که اجرا می‌شود (مثلاً main.py). برایِ dockerfile خالی است.
    entrypoint: Mapped[str | None] = mapped_column(String(200))

    # created | building | running | stopped | failed | suspended
    status: Mapped[str] = mapped_column(String(20), default="created", index=True)
    # آخرین خطا (خطایِ بیلد یا کرشِ کانتینر) برایِ نمایش به کاربر
    last_error: Mapped[str | None] = mapped_column(Text)

    # ── مالی ──
    # لحظهٔ اولین اجرا؛ مبنایِ مهلتِ رایگانِ یک‌ساعته
    first_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    # تا این لحظه پرداخت شده؛ None یعنی هنوز پولی نداده
    paid_until: Mapped[datetime | None] = mapped_column(DateTime)
    # برایِ اینکه یادآوریِ پایانِ مهلت فقط یک‌بار برود
    notice_sent: Mapped[str | None] = mapped_column(String(20))

    # ── منابع (قابلِ ارتقا در پلن‌هایِ بالاتر) ──
    mem_mb: Mapped[int] = mapped_column(Integer, default=512)
    cpu_percent: Mapped[int] = mapped_column(Integer, default=50)
    disk_mb: Mapped[int] = mapped_column(Integer, default=1024)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class HostedAppEnv(Base):
    """متغیرهایِ محیطیِ یک اپ (همان چیزی که در .env می‌نشیند).

    جدا از خودِ فایل نگه داشته می‌شود تا اگر کاربر زیپِ تازه‌ای آپلود کرد
    که .env نداشت، تنظیماتش از بین نرود — این دقیقاً همان موردی است که
    کاربر می‌خواست دکمهٔ جداگانه برایش باشد.
    """
    __tablename__ = "hosted_app_envs"
    __table_args__ = (UniqueConstraint("app_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("hosted_apps.id"), index=True)
    key: Mapped[str] = mapped_column(String(100))
    value: Mapped[str] = mapped_column(Text, default="")


# === PATCH_TENANT_PREMIUM_EMOJI_V1 ===
class TenantPremiumEmoji(Base):
    """نگاشتِ «ایموجیِ معمولی → ایموجیِ پرمیوم» که مالکِ یک ربات مستأجر از
    تنظیماتِ ربات خودش ثبت می‌کند (بخشِ «⭐ افزودن ایموجی پرمیوم»).

    هر ردیف یعنی: هرجا متنِ خروجیِ همین ربات به‌جایِ normal_emoji باید
    ایموجیِ پرمیومِ premium_emoji_id (با کاراکترِ نمایشیِ premium_display)
    نشان داده شود. یک normal_emoji برایِ هر ربات فقط یک ردیف دارد — ثبتِ
    دوبارهٔ همان ایموجیِ معمولی، ردیفِ قبلی را آپدیت می‌کند (تازه‌ترین
    آیدیِ پرمیوم فعال است).
    """
    __tablename__ = "tenant_premium_emojis"
    __table_args__ = (UniqueConstraint("bot_id", "normal_emoji"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    normal_emoji: Mapped[str] = mapped_column(String(32))
    premium_emoji_id: Mapped[str] = mapped_column(String(64))
    # کاراکترِ نمایشی‌ای که تلگرام پشتِ آیدیِ پرمیوم فرستاد (برایِ ساختِ
    # entity هنگامِ استفادهٔ بعدی؛ روی کلاینت‌هایِ غیرپرمیوم همین کاراکتر
    # دیده می‌شود).
    premium_display: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# === PATCH_PLATFORM_PREMIUM_EMOJI_V1 ===
class PlatformSetting(Base):
    """تنظیماتِ کلید/مقدارِ خودِ رباتِ اصلیِ بات‌ساز (پلتفرم/مادر) — مشابهِ
    Setting برایِ ربات‌های مستأجر، ولی بدونِ bot_id چون فقط یک رباتِ
    پلتفرم وجود دارد."""
    __tablename__ = "platform_settings"
    __table_args__ = (UniqueConstraint("key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(60))
    value: Mapped[str] = mapped_column(Text)


class PlatformPremiumEmoji(Base):
    """نگاشتِ «ایموجیِ معمولی → ایموجیِ پرمیوم» برایِ خودِ رباتِ اصلیِ
    بات‌ساز (نه ربات‌های مستأجر — همان جدولِ TenantPremiumEmoji است).
    فقط مدیرِ اصلیِ پلتفرم می‌تواند این‌ها را از پنلِ ادمین مدیریت کند."""
    __tablename__ = "platform_premium_emojis"
    __table_args__ = (UniqueConstraint("normal_emoji"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    normal_emoji: Mapped[str] = mapped_column(String(32))
    premium_emoji_id: Mapped[str] = mapped_column(String(64))
    premium_display: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# === PATCH_WEB_PANEL_V1 ===
class AdminWebSession(Base):
    """سشنِ ورودِ مالک/ادمینِ یک ربات به «پنل تحت وب».

    ورود با رمز نیست: مالک داخلِ خودِ رباتش دکمهٔ ورود را می‌زند، یک توکنِ
    یک‌بارمصرفِ کوتاه‌عمر ساخته می‌شود و لینکش برایش می‌آید. با بازکردنِ
    لینک، توکن مصرف (used_at پر) و یک توکنِ سشن ساخته می‌شود که در کوکی
    می‌نشیند. این‌طوری هیچ رمزی جایی ذخیره یا منتقل نمی‌شود و مالکیت از
    طریقِ خودِ تلگرام اثبات می‌شود.
    """
    __tablename__ = "admin_web_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    login_token: Mapped[str | None] = mapped_column(String(64), index=True)
    session_token: Mapped[str | None] = mapped_column(String(64), index=True)
    login_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    session_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# === PATCH_GROUP_BETTING_V1 ===
class Bet(Base):
    """شرط‌بندی گروهی؛ مبلغ سازنده در زمان ایجاد رزرو می‌شود."""
    __tablename__ = "group_bets"
    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    creator_user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    creator_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    creator_name: Mapped[str] = mapped_column(String(120), default="")
    creator_username: Mapped[str | None] = mapped_column(String(64))
    target_telegram_id: Mapped[int] = mapped_column(BigInteger)
    target_name: Mapped[str] = mapped_column(String(120), default="")
    target_username: Mapped[str | None] = mapped_column(String(64))
    opponent_user_id: Mapped[int | None] = mapped_column(ForeignKey("bot_users.id"), index=True)
    opponent_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    opponent_name: Mapped[str | None] = mapped_column(String(120))
    opponent_username: Mapped[str | None] = mapped_column(String(64))
    amount: Mapped[float] = mapped_column(Numeric(12, 0))
    fee_pct: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    fee_amount: Mapped[float] = mapped_column(Numeric(12, 0), default=0)
    payout_amount: Mapped[float] = mapped_column(Numeric(12, 0), default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    winner_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    winner_name: Mapped[str | None] = mapped_column(String(120))
    winner_username: Mapped[str | None] = mapped_column(String(64))
    loser_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    loser_name: Mapped[str | None] = mapped_column(String(120))
    loser_username: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime)


class BetLedger(Base):
    """دفتر تغییرات هر شرط برای جلوگیری از تسویهٔ مبهم و امکان حسابرسی."""
    __tablename__ = "group_bet_ledger"
    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    bet_id: Mapped[int] = mapped_column(ForeignKey("group_bets.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    amount: Mapped[float] = mapped_column(Numeric(12, 0))
    kind: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# === PATCH_PLATFORM_GROUP_BETTING_V1 ===
class PlatformBet(Base):
    """شرط‌بندی گروهیِ خودِ بات‌ساز؛ متصل به PlatformUser."""
    __tablename__ = "platform_group_bets"
    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    creator_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    creator_name: Mapped[str] = mapped_column(String(120), default="")
    creator_username: Mapped[str | None] = mapped_column(String(64))
    target_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_name: Mapped[str] = mapped_column(String(120), default="")
    target_username: Mapped[str | None] = mapped_column(String(64))
    opponent_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    opponent_name: Mapped[str | None] = mapped_column(String(120))
    opponent_username: Mapped[str | None] = mapped_column(String(64))
    amount: Mapped[float] = mapped_column(Numeric(14, 0))
    fee_pct: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    fee_amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    payout_amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    winner_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    winner_name: Mapped[str | None] = mapped_column(String(120))
    winner_username: Mapped[str | None] = mapped_column(String(64))
    loser_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    loser_name: Mapped[str | None] = mapped_column(String(120))
    loser_username: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime)


class PlatformBetLedger(Base):
    """دفتر تراکنش مستقل شرط‌بندی بات‌ساز اصلی."""
    __tablename__ = "platform_group_bet_ledger"
    id: Mapped[int] = mapped_column(primary_key=True)
    bet_id: Mapped[int] = mapped_column(ForeignKey("platform_group_bets.id"), index=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 0))
    kind: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# === PATCH_PLATFORM_WAGER_V1 ===
class PlatformWager(Base):
    """شرط‌بندی دونفرهٔ گروهی با کیف‌پول بات‌ساز (ایکس‌او / سنگ‌کاغذ‌قیچی / تاس).

    مبلغ هر دو نفر هنگام ساخت/پیوستن از کیف‌پول رزرو می‌شود و فقط با تسویه
    (برنده)، مساوی (برگشت هر دو) یا لغو/انقضا (برگشت سازنده) آزاد می‌شود.
    state = JSON وضعیت بازی (صفحهٔ ایکس‌او، انتخاب‌های سنگ‌کاغذ‌قیچی، تاس‌ها).
    """
    __tablename__ = "platform_wagers"
    id: Mapped[int] = mapped_column(primary_key=True)
    # ۰ = ربات اصلی بات‌ساز (کیف‌پول PlatformUser)؛ غیر صفر = ربات مستأجر (کیف‌پول BotUser همان ربات)
    bot_id: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    game: Mapped[str] = mapped_column(String(8))                    # xo | rps | dice
    creator_id: Mapped[int] = mapped_column(BigInteger, index=True)
    creator_name: Mapped[str] = mapped_column(String(120), default="")
    creator_username: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[int] = mapped_column(BigInteger, default=0)   # ۰ = عمومی
    target_name: Mapped[str] = mapped_column(String(120), default="")
    target_username: Mapped[str | None] = mapped_column(String(64))
    opponent_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    opponent_name: Mapped[str | None] = mapped_column(String(120))
    opponent_username: Mapped[str | None] = mapped_column(String(64))
    amount: Mapped[float] = mapped_column(Numeric(14, 0))
    fee_pct: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    fee_amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    payout_amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)  # pending|playing|settled|draw|cancelled|expired
    state: Mapped[str] = mapped_column(Text, default="{}")
    turn_id: Mapped[int | None] = mapped_column(BigInteger)         # نوبت چه کسی (ایکس‌او)
    winner_id: Mapped[int | None] = mapped_column(BigInteger)
    end_reason: Mapped[str | None] = mapped_column(String(16))      # win | forfeit | draw | cancel | expire
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)  # مهلت پیوستن یا مهلت حرکت بعدی
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    settled_at: Mapped[datetime | None] = mapped_column(DateTime)


# === PATCH_WAGER_ADDON_V1 ===
class BotAddon(Base):
    """افزونهٔ پولی فعال‌شده برای یک ربات مستأجر (مثلا بازی‌های شرط‌بندی). یک‌بار پرداخت، دائمی؛
    جدول جدا تا «ریست ربات» یا تغییر تنظیمات آن را پاک نکند."""
    __tablename__ = "bot_addons"
    __table_args__ = (UniqueConstraint("bot_id", "addon", name="uq_bot_addon"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, index=True)
    addon: Mapped[str] = mapped_column(String(32))
    amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)    # مبلغ پرداخت‌شده (تومان)
    paid_by: Mapped[int] = mapped_column(BigInteger, default=0)         # تلگرام‌آیدی پرداخت‌کننده
    source: Mapped[str] = mapped_column(String(16), default="wallet")   # wallet | free | admin
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# === PATCH_GROUP_REPORT_V1 ===
class GroupReport(Base):
    """گزارش یک عضو توسط عضو دیگر در گروه محافظ‌شدهٔ ربات اصلی (ریپلای + «گزارش»)؛ به مدیر اصلی می‌رسد."""
    __tablename__ = "group_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    reporter_id: Mapped[int] = mapped_column(BigInteger, index=True)
    reporter_name: Mapped[str] = mapped_column(String(120), default="")
    reporter_username: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_name: Mapped[str] = mapped_column(String(120), default="")
    target_username: Mapped[str | None] = mapped_column(String(64))
    target_message_id: Mapped[int] = mapped_column(BigInteger, default=0)    # پیام گزارش‌شده
    message_text: Mapped[str] = mapped_column(Text, default="")              # خلاصهٔ متن پیام گزارش‌شده
    command_message_id: Mapped[int] = mapped_column(BigInteger, default=0)   # پیام «گزارش» خود گزارش‌دهنده
    ack_message_id: Mapped[int] = mapped_column(BigInteger, default=0)       # پیام تأیید ربات در گروه
    prompt_message_id: Mapped[int] = mapped_column(BigInteger, default=0, index=True)  # «دلیل را بنویس»
    admin_message_id: Mapped[int] = mapped_column(BigInteger, default=0)     # پیام گزارش در پیوی مدیر
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="draft", index=True)  # draft|sent|closed|cancelled|failed
    actions: Mapped[str] = mapped_column(String(60), default="")             # warn,mute,del,ban,ok
    handled_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)


# === PATCH_TENANT_GROUP_GUARD_V1 ===
class TenantGroupBan(Base):
    __tablename__ = "tenant_group_guard_bans"
    __table_args__ = (UniqueConstraint("bot_id", "chat_id", "user_id", name="uq_tgg_ban_bot_chat_user"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    full_name: Mapped[str | None] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(String(255))
    banned_by: Mapped[int | None] = mapped_column(BigInteger)
    banned_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TenantGroupWarn(Base):
    __tablename__ = "tenant_group_guard_warns"
    __table_args__ = (UniqueConstraint("bot_id", "chat_id", "user_id", name="uq_tgg_warn_bot_chat_user"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    full_name: Mapped[str | None] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(64))
    count: Mapped[int] = mapped_column(Integer, default=0)
    last_reason: Mapped[str | None] = mapped_column(String(255))
    last_warned_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class TenantGroupCaptcha(Base):
    __tablename__ = "tenant_group_guard_captchas"
    __table_args__ = (UniqueConstraint("bot_id", "chat_id", "user_id", name="uq_tgg_captcha_bot_chat_user"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger)
    full_name: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class TenantGroupEvent(Base):
    __tablename__ = "tenant_group_guard_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    target_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    details: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


# === PATCH_NFT_MARKET_V1 ===
# «مینی‌اپ ۲ / مارکتِ NFT» — مخصوصِ ربات‌هایِ خدماتِ مجازی (پلنِ حرفه‌ای).
# موجودی مالِ خودِ ادمینِ مستأجر است؛ هر آیتم یا فروشی است یا اجاره‌ای یا هر دو.


class NftItem(Base):
    """یک آیتمِ مارکت: گیفتِ NFT یا یوزرنیم، متعلق به ادمینِ همان ربات."""
    __tablename__ = "nft_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="gift")   # gift / username
    title: Mapped[str] = mapped_column(String(120))
    # شمارهٔ آیتمِ کالکشن (#1841) یا خودِ یوزرنیم بدونِ @
    ref: Mapped[str | None] = mapped_column(String(120), index=True)
    # مشخصاتِ نمایشی (مثلِ StarsDex: مدل/رنگ/سمبل/کالکشن)
    collection: Mapped[str | None] = mapped_column(String(80), index=True)
    model: Mapped[str | None] = mapped_column(String(80))
    color: Mapped[str | None] = mapped_column(String(80), index=True)
    symbol: Mapped[str | None] = mapped_column(String(80))
    # تصویر: یا file_id تلگرام یا URLِ مستقیم
    image_file_id: Mapped[str | None] = mapped_column(String(300))
    image_url: Mapped[str | None] = mapped_column(String(500))
    # حالتِ عرضه
    mode: Mapped[str] = mapped_column(String(10), default="both")   # buy / rent / both
    sale_price: Mapped[float | None] = mapped_column(Numeric(14, 0))        # تومان
    rent_daily: Mapped[float | None] = mapped_column(Numeric(14, 0))        # تومان/روز
    rent_min_days: Mapped[int] = mapped_column(Integer, default=1)
    rent_max_days: Mapped[int] = mapped_column(Integer, default=180)
    # وضعیت
    status: Mapped[str] = mapped_column(String(20), default="available", index=True)
    # available / reserved / sold / rented / hidden
    # مالکِ فعلی (خریدار یا مستأجر) — BotUser.id
    holder_user_id: Mapped[int | None] = mapped_column(ForeignKey("bot_users.id"), index=True)
    rent_until: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    # شناسهٔ گیفتِ تلگرام برایِ انتقالِ خودکار (owned_gift_id در Bot API)
    owned_gift_id: Mapped[str | None] = mapped_column(String(120))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # === PATCH_NFT_SOURCES_V1 === آیتم‌هایِ واردشده از منبعِ بیرونی
    # (قبلاً این ستون‌ها اشتباهاً زیرِ MiniAppProductOrder افتاده بودند؛ در نتیجه NftItem.source_id/cost_price/
    # created_at وجود نداشت و _item_json(owner=True) بعد از ذخیره ۵۰۰ می‌داد → ادمین چندبار «ذخیره» می‌زد.)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("nft_market_sources.id"), index=True)
    # شناسهٔ آیتم در مبدأ (برایِ تطبیقِ دوباره هنگامِ همگام‌سازی)
    source_ref: Mapped[str | None] = mapped_column(String(120), index=True)
    # قیمتِ خریدِ مبدأ (تومان) — برایِ محاسبه و نمایشِ سود
    cost_price: Mapped[float | None] = mapped_column(Numeric(14, 0))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # === PATCH_MINIAPP_MANUAL_PRODUCTS_V1 ===
# «محصولاتِ دستیِ مینی‌اپ» — درخواستِ مستأجر: بتواند خودش سرویسِ دلخواه
# (با نام، عکس و نرخ) تعریف کند و مشتری همان‌جا در مینی‌اپ بخرد، بدونِ
# اینکه به فلویِ آمادهٔ گیفت/استارز/SMM وابسته باشد.
# تحویل دستی است: سفارش ثبت و به ادمین اطلاع داده می‌شود.


class MiniAppProduct(Base):
    """یک محصولِ دستیِ تعریف‌شده توسطِ خودِ مستأجر."""
    __tablename__ = "miniapp_products"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(String(400))
    image_url: Mapped[str | None] = mapped_column(String(500))
    price: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    # اگر پر باشد، از مشتری یک ورودی می‌گیرد (مثلاً «لینکِ پست» یا
    # «یوزرنیمِ مقصد») و همان را برایِ ادمین می‌فرستد.
    input_label: Mapped[str | None] = mapped_column(String(120))
    # موجودی: None یعنی نامحدود
    stock: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MiniAppProductOrder(Base):
    """سفارشِ یک محصولِ دستی. تحویل با خودِ ادمین است."""
    __tablename__ = "miniapp_product_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("miniapp_products.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    title_snapshot: Mapped[str] = mapped_column(String(120))
    amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    user_input: Mapped[str | None] = mapped_column(String(400))
    tracking_code: Mapped[str | None] = mapped_column(String(20), index=True)
    # pending / delivered / rejected
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    admin_note: Mapped[str | None] = mapped_column(String(400))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)




class NftOrder(Base):
    """سفارشِ خرید یا اجارهٔ یک آیتمِ مارکت."""
    __tablename__ = "nft_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("nft_items.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10), default="buy")    # buy / rent
    days: Mapped[int] = mapped_column(Integer, default=0)           # فقط برایِ اجاره
    amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)        # مبلغِ پرداختی
    fee: Mapped[float] = mapped_column(Numeric(14, 0), default=0)           # کارمزدِ انتقالِ تلگرام
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # pending / paid / delivered / returned / rejected / expired
    # auto = انتقالِ خودکار با Business API، manual = تحویلِ دستیِ ادمین
    transfer_mode: Mapped[str] = mapped_column(String(10), default="manual")
    # متنِ خطایِ انتقالِ خودکار (اگر شکست خورد و به دستی برگشت)
    transfer_note: Mapped[str | None] = mapped_column(String(400))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    # === PATCH_NFT_TGTOOLS_V1 === خریدِ خودکار از TGTools: آدرسِ TONِ دریافت‌کننده + شناسهٔ سفارش در مبدأ
    recipient_ton: Mapped[str | None] = mapped_column(String(120))
    source_order_id: Mapped[str | None] = mapped_column(String(40), index=True)


class NftBizConn(Base):
    """اتصالِ اکانتِ بیزینسِ ادمین به ربات — لازم برایِ انتقالِ خودکارِ گیفت."""
    __tablename__ = "nft_biz_conns"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    connection_id: Mapped[str] = mapped_column(String(120), index=True)
    user_chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    can_transfer: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# === PATCH_NFT_SOURCES_V1 ===
# «منابعِ مارکت» — واسطه‌گری: آیتم‌ها از یک مارکتِ بیرونی (فعلاً Tonnel)
# خوانده می‌شوند، با درصدِ سودِ فروشنده قیمت‌گذاری و در مینی‌اپ عرضه می‌شوند.
# طراحیِ چندمنبعی است (مارکت ۱، مارکت ۲، …) تا افزودنِ مارکتِ بعدی فقط
# یک provider تازه در tenant/nft_sources.py باشد، نه تغییر در مدل و UI.


class NftMarketSource(Base):
    """یک منبعِ بیرونیِ آیتم برایِ مارکتِ NFT."""
    __tablename__ = "nft_market_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    provider: Mapped[str] = mapped_column(String(30), default="tonnel")
    title: Mapped[str | None] = mapped_column(String(80))
    # authDataی مارکت — رمزنگاری‌شده (همان wallet_crypto)
    encrypted_auth: Mapped[str | None] = mapped_column(Text)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # درصدِ سودی که رویِ قیمتِ مبدأ کشیده می‌شود
    markup_percent: Mapped[float] = mapped_column(Numeric(6, 2), default=15)
    # آیتمی که در مبدأ ناموجود شد، خودکار مخفی شود؟
    auto_hide_missing: Mapped[bool] = mapped_column(Boolean, default=True)
    # فقط این کالکشن‌ها همگام شوند (خالی = همه)
    collection_filter: Mapped[str | None] = mapped_column(String(300))
    max_items: Mapped[int] = mapped_column(Integer, default=60)
    # سلامتِ توکن: ok / expired / error / unknown
    token_state: Mapped[str] = mapped_column(String(20), default="unknown")
    last_error: Mapped[str | None] = mapped_column(String(400))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime)
    # آخرین باری که دربارهٔ انقضایِ توکن به ادمین هشدار داده شده
    expiry_warned_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# === PATCH_P2P_MARKET_V1 ===
# «بازارِ کاربران» داخلِ مارکتِ NFT: کاربرها گیفت/یوزرنیمِ خودشان را می‌فروشند
# یا اجاره می‌دهند؛ ربات واسطه (اسکرو) است: پولِ خریدار از کیف‌پولش کم و نزدِ
# ربات نگه داشته می‌شود تا فروشنده انتقال بدهد و خریدار تأیید کند.


class P2PListing(Base):
    """آگهیِ یک کاربر در بازارِ کاربران."""
    __tablename__ = "p2p_listings"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    seller_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="gift")        # gift / username
    title: Mapped[str] = mapped_column(String(120))
    ref: Mapped[str | None] = mapped_column(String(120))                 # شمارهٔ گیفت یا یوزرنیم
    link: Mapped[str | None] = mapped_column(String(300))                # t.me/nft/...
    collection: Mapped[str | None] = mapped_column(String(80), index=True)
    model: Mapped[str | None] = mapped_column(String(80))
    color: Mapped[str | None] = mapped_column(String(80), index=True)
    symbol: Mapped[str | None] = mapped_column(String(80))
    image_url: Mapped[str | None] = mapped_column(String(500))
    mode: Mapped[str] = mapped_column(String(10), default="buy")         # buy / rent / both
    sale_price: Mapped[float | None] = mapped_column(Numeric(14, 0))
    rent_daily: Mapped[float | None] = mapped_column(Numeric(14, 0))
    rent_min_days: Mapped[int] = mapped_column(Integer, default=1)
    rent_max_days: Mapped[int] = mapped_column(Integer, default=30)
    note: Mapped[str | None] = mapped_column(String(300))
    # available / reserved / sold / rented / hidden / removed
    status: Mapped[str] = mapped_column(String(20), default="available", index=True)
    rent_until: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class P2PDeal(Base):
    """یک معاملهٔ اسکرو بینِ خریدار و فروشنده."""
    __tablename__ = "p2p_deals"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("p2p_listings.id"), index=True)
    buyer_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    seller_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10), default="buy")         # buy / rent
    days: Mapped[int] = mapped_column(Integer, default=0)
    price: Mapped[float] = mapped_column(Numeric(14, 0), default=0)      # مبلغِ نگه‌داشته‌شده
    title_snapshot: Mapped[str] = mapped_column(String(120))
    # escrow → (فروشنده انتقال داد + کارمزد) await_buyer → completed
    # escrow → cancelled (فروشنده/تایم‌اوت) ؛ await_buyer → disputed → released/refunded
    status: Mapped[str] = mapped_column(String(20), default="escrow", index=True)
    fee_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    fee_method: Mapped[str | None] = mapped_column(String(10))           # stars / wallet / free
    fee_amount: Mapped[int] = mapped_column(Integer, default=0)          # استارز یا تومان
    note: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class P2PWithdrawal(Base):
    """درخواستِ برداشتِ مبلغِ فروشِ یک کاربر؛ تسویه را ادمین انجام می‌دهد."""
    __tablename__ = "p2p_withdrawals"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 0), default=0)
    card_number: Mapped[str] = mapped_column(String(32))
    card_holder: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending/paid/rejected
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)


# === PATCH_TENANT_OWNED_MODELS_MISSING_TABLES_V3 ===
# ۲۱ جدولِ زیر FK به bots.id دارند ولی هیچ‌وقت به TENANT_OWNED_MODELS اضافه
# نشده بودند. پرتکرارترین خطای production دقیقاً از همین‌جا می‌آمد:
#     gift_orders_buyer_user_id_fkey violates foreign key constraint
# (۸۴ بار در error.log) — یعنی «حذفِ ربات» و «تعویضِ توکن» شکست می‌خورد.
#
# چرا اینجا و نه بالا، کنارِ خودِ لیست؟ چون بخشی از این کلاس‌ها (Bet،
# BetLedger، TelegramGiftConfig و ...) *پایین‌ترِ* آن لیست تعریف شده‌اند و
# نامشان آن بالا هنوز وجود ندارد (NameError موقعِ import). الحاق در انتهایِ
# فایل تضمین می‌کند همهٔ کلاس‌ها تعریف شده‌اند.
#
# ترتیب مهم نیست: حلقهٔ purge_bot در config.py چندپاسه است و هر مدلی که
# به‌خاطرِ FK شکست بخورد به دورِ بعد موکول می‌شود.
#
# PartnerBot/PartnerSale عمداً اینجا نیستند: بلوکِ PATCH_PURGE_PARTNER_LINKS_V1
# در config.py آن‌ها را جداگانه و با حفظِ سوابقِ مالی (NULL کردنِ ارجاع به‌جایِ
# حذف) مدیریت می‌کند. PlanOrder هم جداگانه فقط target_bot_id‌اش NULL می‌شود.
TENANT_OWNED_MODELS += [
    GiftOrder, TelegramGiftConfig,
    Bet, BetLedger,                        # شرط‌بندیِ گروهی
    KycRequest, RefPointWallet,
    SmmPanel, SmmCategory,
    HesabPayCharge, TonDirectCharge, UniquePayCharge, ApayCharge, AbanGatewayCharge,
    CommunityChat, BotCustomDomain, TenantPremiumEmoji,
    TenantGroupBan, TenantGroupWarn, TenantGroupEvent, TenantGroupCaptcha,
    AdminWebSession, ResellerTrialLog, VpnPartner,
    # === PATCH_NFT_MARKET_V1 ===
    # ترتیب مهم است: NftOrder به NftItem ارجاع دارد، پس اول سفارش‌ها حذف
    # شوند بعد آیتم‌ها (حلقهٔ purge_bot چندپاسه است ولی این‌جوری یک پاس کمتر).
    NftOrder, NftItem, NftBizConn,
    NftMarketSource,   # === PATCH_NFT_SOURCES_V1 === (بعد از NftItem)
    # === PATCH_MINIAPP_MANUAL_PRODUCTS_V1 === سفارش‌ها قبل از محصول
    MiniAppProductOrder, MiniAppProduct,
    # === PATCH_P2P_MARKET_V1 === معامله‌ها قبل از آگهی‌ها
    P2PDeal, P2PWithdrawal, P2PListing,
]


# === PATCH_MINIAPP_WHEEL_V1 ===
class WheelSpin(Base):
    """هر چرخشِ گردونهٔ شانسِ مینی‌اپ (برایِ محدودیتِ دفعات و گزارش)."""
    __tablename__ = "wheel_spins"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    slice_idx: Mapped[int] = mapped_column(Integer, default=0)
    prize_type: Mapped[str] = mapped_column(String(10), default="none")  # none/balance/pct/amt
    value: Mapped[int] = mapped_column(Integer, default=0)
    code: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# === PATCH_SEQ_TRACKING_V1 ===
# کدِ پیگیریِ سفارش‌ها: یک شمارندهٔ ترتیبیِ مشترک برایِ هر ربات (۱۰۰۱، ۱۰۰۲، …)
# که بینِ همهٔ انواعِ سفارش (کانفیگ، SMM، استارز/گیفت، شماره، محصول، مینی‌اپ)
# یکی است و «ادامهٔ هم» می‌آید. (کدِ پرداخت/شارژ جداست و دست نخورده.)
class TrackingCounter(Base):
    __tablename__ = "tracking_counters"
    bot_id: Mapped[int] = mapped_column(primary_key=True)
    last: Mapped[int] = mapped_column(Integer, default=0)


def _alloc_sync(conn, bot_id: int) -> str:
    t = TrackingCounter.__table__
    for _ in range(4):
        r = conn.execute(t.update().where(t.c.bot_id == bot_id).values(last=t.c.last + 1))
        if r.rowcount:
            return str(conn.execute(select(t.c.last).where(t.c.bot_id == bot_id)).scalar())
        try:
            with conn.begin_nested():
                conn.execute(t.insert().values(bot_id=bot_id, last=0))   # اولین فاکتورِ هر رباتِ تازه = ۱
        except Exception:
            # ردیفِ شمارنده هم‌زمان توسطِ درخواستِ دیگری ساخته شد؛ پایین‌تر UPDATE می‌شود
            pass   # هم‌زمان ساخته شد؛ دوباره UPDATE
    import secrets
    import string
    return "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))


async def alloc_tracking_in(session, bot_id: int) -> str:
    """کدِ بعدی را داخلِ همان تراکنشِ جاری می‌گیرد (برایِ جاهایی که Session باز است)."""
    return await session.run_sync(lambda ss: _alloc_sync(ss.connection(), int(bot_id)))


async def order_no(model, pk) -> str:
    """شناسهٔ واحدِ سفارش (همان کدِ پیگیری) برایِ نمایش؛ اگر نبود، خودِ pk."""
    try:
        from config import Session as _S
        async with _S() as _s:
            t = await _s.scalar(select(model.tracking_code).where(model.id == int(pk)))
        return str(t or pk)
    except Exception:  # noqa: BLE001
        return str(pk)


async def next_tracking(bot_id: int) -> str:
    """کدِ بعدی را با تراکنشِ جداگانه می‌گیرد (وقتی Session بازی نیست)."""
    from config import Session as _S
    async with _S() as s:
        code = await alloc_tracking_in(s, bot_id)
        await s.commit()
    return code


def _install_tracking_defaults() -> None:
    from sqlalchemy import event

    def _fill(mapper, connection, target):
        if not getattr(target, "tracking_code", None):
            target.tracking_code = _alloc_sync(connection, int(getattr(target, "bot_id", 0) or 0))

    for _cls in (Order, ProductOrder, GiftOrder, SmmOrder, MiniAppProductOrder):
        event.listen(_cls, "before_insert", _fill)


_install_tracking_defaults()


# === PATCH_VPN_REFUND_APPROVAL_V1 ===
class VpnRefundRequest(Base):
    """درخواستِ برگشت وجهِ یک سرویسِ VPN که باید ادمین تأیید کند (دیگر خودکار نیست)."""
    __tablename__ = "vpn_refund_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    account_id: Mapped[int] = mapped_column(Integer, index=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    reason: Mapped[str] = mapped_column(String(600), default="")
    quoted_net: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)  # pending/processing/approved/rejected
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    decided_by: Mapped[int | None] = mapped_column(BigInteger)


# === PATCH_PURGE_COMMUNITY_CHILDREN_V1 === جدول‌هایی که bot_id دارند ولی در فهرستِ حذف نبودند
TENANT_OWNED_MODELS += [WheelSpin, TrackingCounter, VpnRefundRequest]


# === PATCH_FUNSTAT_V1 === ربات/بخشِ «Fun Stat» در بات‌سازِ اصلی (تست با دسترسیِ دستی)
class FunstatAccess(Base):
    """دسترسیِ یک کاربر به بخشِ Fun Stat (ادمین می‌دهد؛ expires_at=None یعنی دائمی)."""
    __tablename__ = "funstat_access"
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    granted_by: Mapped[int | None] = mapped_column(BigInteger)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    note: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class FunstatRequest(Base):
    """درخواستِ دسترسیِ کاربر (pending / approved / rejected)."""
    __tablename__ = "funstat_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class FunstatLog(Base):
    """ثبتِ هر جست‌وجو (برایِ سقفِ روزانه، هزینهٔ سرویس و بازرسیِ سوءاستفاده)."""
    __tablename__ = "funstat_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(30))
    query: Mapped[str | None] = mapped_column(String(200))
    cost: Mapped[float] = mapped_column(Float, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class PlatformPromoter(Base):
    """=== PATCH_PLATFORM_PROMOTER_V1 === معرفِ فروش: هر کس با لینک/کدِ او بیاید، روی
    «خریدِ اولِ» خودش درصدِ مشخصی تخفیف می‌گیرد."""
    __tablename__ = "platform_promoters"
    __table_args__ = (UniqueConstraint("code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(80))
    percent: Mapped[int] = mapped_column(Integer, default=10)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PlatformPromoUse(Base):
    """مصرفِ تخفیفِ معرف — برایِ هر کاربر فقط یک‌بار (UniqueConstraint)."""
    __tablename__ = "platform_promo_uses"
    __table_args__ = (UniqueConstraint("user_telegram_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    promoter_id: Mapped[int] = mapped_column(Integer, index=True)
    percent: Mapped[int] = mapped_column(Integer, default=0)
    paid_toman: Mapped[int] = mapped_column(Integer, default=0)
    discount_toman: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PlatformUserDiscount(Base):
    """=== PATCH_PLATFORM_USER_DISCOUNT_V1 === تخفیفِ دائمیِ اختصاصیِ یک کاربر در رباتِ مادر
    (مدیر آیدیِ عددی و درصد می‌دهد؛ روی هر خرید/تمدیدِ پلن اعمال می‌شود تا خاموش یا صفر شود)."""
    __tablename__ = "platform_user_discounts"
    __table_args__ = (UniqueConstraint("user_telegram_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    percent: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(80))
    # پلن‌هایی که تخفیف روی آن‌ها اعمال می‌شود (growth,pro)؛ خالی/None = همهٔ پلن‌ها
    plans: Mapped[str | None] = mapped_column(String(60))
    # درصدِ جداگانه برایِ هر پلن، مثل «growth:13,pro:20»؛ اگر پر باشد بر percent/plans می‌چربد
    plan_percents: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
