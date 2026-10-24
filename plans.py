"""
پلن‌ها و سیستم قفل قابلیت‌ها (feature gating).

هستهٔ کل پلتفرم اینجاست: هر قابلیت یک «کلید» دارد. اینکه یک قابلیت
برای یک ربات باز است یا قفل، فقط به پلن آن ربات بستگی دارد.
برای اضافه‌کردن یک قابلیت جدید فقط کافیست کلیدش را به پلن(ها) اضافه کنیم.

مدیر اصلی می‌تواند از پنل مدیریت، دسترسی هر بخش را برای هر پلن باز/بسته کند.
"""
from enum import Enum


class Feature(str, Enum):
    # --- قابلیت‌های پلن رایگان (پایه) ---
    KEYBOARD_BUTTONS = "keyboard_buttons"      # دکمه‌های کیبورد
    INLINE_BUTTONS = "inline_buttons"          # دکمه‌های اینلاین
    WALLET = "wallet"                          # کیف پول
    MANUAL_INVENTORY = "manual_inventory"      # انبار دستی کانفیگ
    STOCK_NOTIFY = "stock_notify"              # لیست انتظار و اطلاع‌رسانی موجودی
    PURCHASE_FLOW = "purchase_flow"            # خرید با +/- (حجم/روز/تعداد)
    CARD_TO_CARD = "card_to_card"              # کارت‌به‌کارت + رسید + تایید ادمین
    ADMIN_PANEL = "admin_panel"                # پنل مدیریت
    BOT_STATS = "bot_stats"                    # آمار ربات
    EDIT_CONFIG = "edit_config"                # ویرایش کانفیگ با کد اختصاصی
    USER_LOOKUP = "user_lookup"                # جستجو/پروفایل کاربر، شارژ/کسر، بن
    QR_DELIVERY = "qr_delivery"               # تحویل کانفیگ + QR
    EDITABLE_BUTTONS = "editable_buttons"      # ویرایش متن/نام/محدودیت دکمه‌ها

    # --- قابلیت‌های پولی (رشد / حرفه‌ای) ---
    PANEL_INTEGRATION = "panel_integration"    # اتصال به Marzban/Hiddify/X-UI
    COLORED_BUTTONS = "colored_buttons"        # دکمه‌های رنگی aiogram جدید
    # === PATCH_FREE_PLAN_SIMPLIFY_V1 ===
    # پلنِ رایگان باید «سادهٔ جذاب» باشد، نه یک پنلِ شلوغ با ده‌ها گزینه‌ای
    # که هیچ‌کدام برایش کار نمی‌کند. تا امروز این‌ها بی‌هیچ گِیتی به رایگان
    # هم نشان داده می‌شدند (زرین‌پال، یونیک‌پی، استارز، احرازِ هویت، ...).
    # هر کدام از رشد به بالا؛ رفتارِ پلن‌هایِ پولی ذره‌ای عوض نمی‌شود.
    GATEWAY_ZARINPAL     = "gateway_zarinpal"       # درگاهِ زرین‌پال
    # === PATCH_ZIBAL_GATEWAY_V1 === طبقِ خواستهٔ ادمین: فقط پلنِ حرفه‌ای.
    GATEWAY_ZIBAL        = "gateway_zibal"          # درگاهِ زیبال
    GATEWAY_UNIQUEPAY    = "gateway_uniquepay"      # درگاهِ یونیک‌پی
    GATEWAY_APAY         = "gateway_apay"           # درگاهِ آپی (کارت‌به‌کارتِ آنی)
    # === PATCH_ABANGATEWAY_V1 === طبقِ خواستهٔ مدیر: مثلِ یونیک‌پی، رشد و حرفه‌ای.
    GATEWAY_ABANGATEWAY  = "gateway_abangateway"     # درگاهِ آبان گیت‌وی
    GATEWAY_STARS        = "gateway_stars"          # پرداخت با استارزِ تلگرام
    GATEWAY_HESABPAY     = "gateway_hesabpay"       # درگاهِ حساب‌پی (افغانی)
    KYC_VERIFY           = "kyc_verify"             # احرازِ هویتِ کاربران
    TX_LOG_CHANNEL       = "tx_log_channel"         # کانالِ لاگِ تراکنش‌ها
    TRIAL_REPORT_CHANNEL = "trial_report_channel"   # کانالِ گزارشِ اکانتِ تست (PATCH_TRIAL_REPORT_CHANNEL_V1)
    ESCROW               = "escrow"                 # واسطه‌گری (معامله با واسطه) — PATCH_ESCROW_V1
    # === PATCH_METERED_PLAN_V1 === بسته‌یِ اعتباری (پرداخت به‌ازایِ مصرف از کیف پول) — فقط حرفه‌ای، فقط VPN
    METERED_PLAN         = "metered_plan"
    BULK_WALLET          = "bulk_wallet"            # شارژ/کسرِ همگانیِ کیف پول
    PHONE_VERIFY         = "phone_verify"           # تاییدِ شماره و کانالِ ثبتِ شماره‌ها
    AUTO_APPROVE_RECEIPT = "auto_approve_receipt"   # تاییدِ خودکارِ رسید
    WALLET_RECEIPT_BTNS  = "wallet_receipt_btns"    # دکمهٔ زیرِ پیامِ تاییدِ واریز
    DICE_GAME            = "dice_game"              # «بازی کن و جایزه بگیر»
    BOT_BACKUP           = "bot_backup"             # بک‌آپِ کاملِ ربات
    ONBOARDING_WIZARD    = "onboarding_wizard"      # راه‌اندازیِ حرفه‌ایِ ورود
    AUTO_SUB = "auto_sub"                      # ساخت خودکار ساب از پنل
    DISCOUNT_CODES = "discount_codes"          # کد تخفیف
    GATEWAY_PAYMENT = "gateway_payment"        # درگاه پرداخت آنلاین
    REFERRAL = "referral"                      # سیستم زیرمجموعه‌گیری

    # --- قابلیت‌های جدید ---
    PRODUCT_SHOP = "product_shop"              # فروشگاه محصولات دیجیتال
    APPOINTMENT = "appointment"                # رزرو نوبت
    POLL = "poll"                              # نظرسنجی و رای‌گیری
    AUTO_MESSAGE = "auto_message"              # پیام‌های خودکار (خوش‌آمد، یادآوری)
    REVIEW_SYSTEM = "review_system"            # سیستم نظر و امتیازدهی
    ADVANCED_STATS = "advanced_stats"          # آمار پیشرفته
    CUSTOM_MENU = "custom_menu"                # منوی سفارشی درختی
    BROADCAST_PHOTO = "broadcast_photo"        # ارسال همگانی با عکس/مدیا
    USER_TAGS = "user_tags"                    # برچسب‌گذاری کاربران
    SCHEDULED_BROADCAST = "scheduled_broadcast" # ارسال زمان‌بندی شده
    CUSTOM_WELCOME = "custom_welcome"          # ویرایش پیام خوش‌آمد ربات
    CHANNEL_POST = "channel_post"              # ارسال پست با دکمه اینلاین به کانال
    TRUST_CHANNEL = "trust_channel"            # ارسال گزارشِ سفارش به کانال اعتمادسازی
    WIREGUARD_CONFIG = "wireguard_config"      # پشتیبانی از کانفیگِ وایرگارد

    # === PATCH_OPENVPN_PLAN_GATE_V1 ===
    # اوپن‌وی‌پی‌ان جدا از وایرگارد گیت می‌شود: وایرگارد از پلنِ پایه هم
    # هست و نباید عقب برود؛ این یکی فقط رشد و حرفه‌ای.
    OPENVPN_CONFIG = "openvpn_config"
    # === PATCH_COMMUNITY_MGMT_V1 ===
    COMMUNITY_MGMT = "community_mgmt"          # مدیریتِ گروه/کانال (ضداسپم/خوشامد/فیلتر/بن)
    # === PATCH_SELF_OWNERSHIP_LOCK_V1 ===
    # تعویضِ توکن و انتقالِ مالکیت به‌دستِ خودِ مالک. رویِ پلنِ رایگان و تا
    # وقتی ربات در دورهٔ تست است بسته می‌ماند (فقط مدیرِ بات‌ساز می‌تواند
    # انجام دهد)؛ با خریدِ پلنِ پولی خودکار باز می‌شود.
    SELF_TOKEN_SWAP = "self_token_swap"        # تعویض توکن / انتقال مالکیت توسط مالک

    # === PATCH_PLAN_FEATURE_GATING_V1 ===
    # سه قابلیتِ تازه‌ای که تا الان بدونِ هیچ محدودیتِ پلنی بودند (همه، حتی
    # پلنِ رایگان، دسترسیِ کامل داشتند) — طبقِ درخواستِ مدیر، حالا هرکدام
    # به یک پلن مشخص محدود می‌شوند.
    QR_BACKGROUND = "qr_background"            # عکسِ دلخواه پشتِ بارکد
    COMBO_BUTTON = "combo_button"               # دکمهٔ خریدِ ترکیبی (چند دسته/پنل)
    REFERRAL_POINTS = "referral_points"         # امتیازگیری با رفرال
    MULTI_PANEL_INSTANCES = "multi_panel_instances"  # چند پنلِ هم‌نوع (پاسارگاد ۱/۲/۳)
    # === PATCH_TON_DIRECT_PRO_ONLY_V1 ===
    # طبقِ خواستهٔ مدیر: واریزِ مستقیمِ TON به ولتِ خودِ مالک (Tonkeeper و
    # هر ولتِ دیگر) فقط در پلنِ حرفه‌ای.
    TON_DIRECT = "ton_direct"                   # پرداخت مستقیم TON به ولتِ مالک

    # === PATCH_TENANT_PREMIUM_EMOJI_V1 ===
    PREMIUM_EMOJI = "premium_emoji"            # افزودن ایموجی پرمیوم (نگاشتِ ایموجیِ معمولی → پرمیوم)

    # === PATCH_TRUST_TEXT_PRO_ONLY_V1 ===
    # «کانالِ اعتمادسازی» خودش از پلنِ رایگان هم هست (Feature.TRUST_CHANNEL
    # در _BASE)، ولی طبقِ خواستهٔ مدیر «ویرایشِ متنِ پیام» — یعنی قالبِ
    # دلخواه با HTML و جاگذاری‌ها — قابلیتی پیشرفته است و فقط در پلنِ
    # حرفه‌ای باز می‌شود؛ رایگان و رشد همان متنِ پیش‌فرض را می‌گیرند.
    TRUST_CHANNEL_TEXT = "trust_channel_text"  # ویرایشِ متنِ پیامِ کانالِ اعتمادسازی
    FORCE_JOIN_TEXT = "force_join_text"        # ویرایشِ متنِ پیامِ عضویتِ اجباری
    # === PATCH_FORCEJOIN_BTN_COLOR_V1 ===
    # رنگ‌کردنِ دکمه‌هایِ پیامِ عضویتِ اجباری، جدا برایِ هر کانال. طبقِ
    # درخواستِ مدیر فقط پلنِ حرفه‌ای — مثلِ FORCE_JOIN_TEXT و GUIDE_BTN_COLOR.
    FORCE_JOIN_COLOR = "force_join_color"

    # === PATCH_PANEL_RELINK_V1 ===
    # بازسازیِ ساب‌لینکِ همهٔ کاربران پس از تعویض/بازسازیِ پنل + اطلاع‌رسانی
    PANEL_RELINK = "panel_relink"

    # === PATCH_NFT_MARKET_V1 ===
    # «مینی‌اپ ۲» — مارکتِ خرید و اجارهٔ گیفتِ NFT و یوزرنیم، مخصوصِ
    # ربات‌هایِ خدماتِ مجازی. طبقِ خواستهٔ مدیر فقط پلنِ حرفه‌ای.
    NFT_MARKET = "nft_market"

    # === PATCH_GUIDE_BTN_COLOR_PRO_V1 ===
    # رنگِ جداگانه برایِ هر دکمهٔ «آموزش اتصال». جدا از COLORED_BUTTONS
    # (که از پلنِ رشد به بالاست) نگه داشته شده تا فقط همین مورد حرفه‌ای
    # باشد و رنگِ بقیهٔ دکمه‌ها برایِ رشد عوض نشود.
    GUIDE_BTN_COLOR = "guide_btn_color"
    # === PATCH_PAYGUIDE_MOVE_FINANCE_PRO_V1 ===
    # دکمهٔ «راهنمای پرداخت و افزایش موجودی» (هم در پنلِ مدیریت، هم برایِ
    # کاربرِ نهایی در صفحهٔ پیش‌فاکتور) — طبقِ خواستهٔ مدیر فقط پلنِ حرفه‌ای.
    PAYMENT_GUIDE = "payment_guide"
    # === PATCH_RESELLER_TIERS_V1 ===
    RESELLER_TIERS = "reseller_tiers"
    # === PATCH_WEB_PANEL_V1 ===
    WEB_PANEL = "web_panel"
    # === PATCH_SMM_MULTI_PANEL_V1 ===
    # اتصالِ چند پنلِ SMM هم‌زمان (نه فقط یک API). طبقِ خواستهٔ مدیر: فقط
    # پلنِ حرفه‌ای. پلن‌های پایین‌تر همچنان همون پنلِ تکیِ قدیمی (بدونِ گیت،
    # همیشه بوده) رو دارن.
    SMM_MULTI_PANEL = "smm_multi_panel"
    # === PATCH_NODEX_PANEL_V1 === طبقِ خواستهٔ ادمین: پنلِ نودکس فقط پلنِ حرفه‌ای.
    NODEX_PANEL = "nodex_panel"
    # === PATCH_DEGERIS_PANEL_V1 === پنلِ DEGERIS (ثنایی جدید) فقط پلنِ حرفه‌ای.
    DEGERIS_PANEL = "degeris_panel"
    # === PATCH_EYLAN_PANEL_V1 === پنلِ ایلان فقط پلنِ حرفه‌ای.
    EYLAN_PANEL = "eylan_panel"
    # === PATCH_HEIMDALL_PANEL_V1 === پنلِ HEIMDALL فقط پلنِ حرفه‌ای.
    HEIMDALL_PANEL = "heimdall_panel"
    # === PATCH_RICKPANEL_PANEL_V1 === پنلِ RickPanel فقط پلنِ حرفه‌ای.
    RICKPANEL_PANEL = "rickpanel_panel"
    # === PATCH_PLAN_TABLE_VIEW_V1 === نمایشِ جدولیِ پلن‌ها (قیمت|زمان|حجم) — فقط پلنِ حرفه‌ای.
    PLAN_TABLE_VIEW = "plan_table_view"
    # === PATCH_VPN_PANEL_MARKET_V1 ===
    # ثبت و فروشِ پنل (نه کانفیگِ VPN) — فقط پلنِ حرفه‌ای، فقط ربات‌هایِ فروشِ VPN.
    VPN_PANEL_MARKET = "vpn_panel_market"
    # === PATCH_PANEL_YAR_MOVE_V1 ===
    # «پنل‌یار» قبلاً یه نوعِ کاملاً جداگانه‌ی ربات بود؛ حالا یه زیرقابلیتِ
    # ربات‌هایِ فروشِ VPN شد — فقط پلنِ حرفه‌ای.
    PANEL_YAR = "panel_yar_tool"

    # === PATCH_SMM_API_ACCESS_V1 ===
    # طبقِ خواستهٔ ادمین: اتصال به هر پنلِ SMM (چه پنلِ پیش‌فرضِ تکی، چه
    # ارائه‌دهنده‌هایِ اضافه) — یعنی کلِ زیرمجموعهٔ «ارائه‌دهندگان»، «مدیریتِ
    # محصولات»، و «مدیریتِ دسته‌ها» — از پلنِ رشد به بالا. پلنِ رایگان فقط
    # «سرویس‌های دستی» (گیفت/پرمیوم/استارز/شماره‌ی مجازی/TON، که اصلاً به
    # هیچ API‌یی وصل نمی‌شن، دستیِ خودِ ادمینن) رو داره. چندپنلی
    # (SMM_MULTI_PANEL، بالاتر) جدا و همچنان فقط پلنِ حرفه‌ای می‌مونه —
    # این فیچر فقط دربارهٔ «اصلِ اتصال به یک پنل» است، نه «چندتا هم‌زمان».
    SMM_API_ACCESS = "smm_api_access"

    # === PATCH_TENANT_HOME_STICKER_V1 ===
    # همون قابلیتِ «افزودنِ استیکر» که در ربات اصلیِ بات‌ساز رویِ استارت و
    # دکمه‌های خانه هست؛ حالا برایِ ربات‌هایِ مستأجر هم اضافه شده، اما فقط
    # پلنِ حرفه‌ای (طبقِ خواستهٔ ادمین) — از «دکمه‌ها و تنظیمات» قابلِ مدیریت است.
    HOME_STICKERS = "home_stickers"

    # === PATCH_GROUP_AI_COMMUNITY_MGMT_V1 ===
    # دستیارِ هوشِ مصنوعیِ گروه برایِ ربات‌هایِ «مدیریتِ گروه و
    # کانال» — همان دستیاری که در virtual_service با تنظیمِ کلیدِ
    # API کار می‌کند؛ فقط پلنِ حرفه‌ای.
    GROUP_AI_ASSISTANT = "group_ai_assistant"


# نام نمایشی هر قابلیت برای پنل مدیریت
FEATURE_LABELS = {
    Feature.KEYBOARD_BUTTONS: "دکمه‌های کیبورد",
    Feature.INLINE_BUTTONS: "دکمه‌های اینلاین",
    Feature.WALLET: "کیف پول",
    Feature.MANUAL_INVENTORY: "انبار دستی کانفیگ",
    Feature.STOCK_NOTIFY: "لیست انتظار و اطلاع‌رسانی موجودی کانفیگ",
    Feature.PURCHASE_FLOW: "جریان خرید (حجم/روز/تعداد)",
    Feature.CARD_TO_CARD: "کارت‌به‌کارت + رسید",
    Feature.ADMIN_PANEL: "پنل مدیریت ربات",
    Feature.BOT_STATS: "آمار ربات",
    Feature.EDIT_CONFIG: "ویرایش کانفیگ با کد",
    Feature.USER_LOOKUP: "مدیریت کاربران",
    Feature.QR_DELIVERY: "تحویل کانفیگ + QR",
    Feature.EDITABLE_BUTTONS: "ویرایش دکمه‌ها",
    Feature.SELF_TOKEN_SWAP: "تعویض توکن و انتقال مالکیت توسط خودِ مالک",
    # === PATCH_PLAN_FEATURE_GATING_V1 ===
    Feature.QR_BACKGROUND: "عکسِ دلخواه پشتِ بارکد (QR)",
    Feature.COMBO_BUTTON: "دکمهٔ خریدِ ترکیبی (چند دسته/پنل با هم)",
    # === PATCH_COIN_DIRECT_MULTI_ASSET_V1 === همان قابلیت، حالا سه ارز.
    # عمداً عضوِ enum تازه‌ای اضافه نشد: ترون و تتر گونه‌هایِ همین
    # قابلیت‌اند، نه یک دسترسیِ جدا که پلن‌ها باید جداگانه بخرندش.
    Feature.TON_DIRECT: "پرداخت مستقیم ارز (تون/ترون/تتر — ولتِ خودِ مالک)",
    Feature.REFERRAL_POINTS: "امتیازگیری با رفرال",
    Feature.MULTI_PANEL_INSTANCES: "چند پنلِ هم‌نوع (مثلاً پاسارگاد ۱، ۲، ۳)",
    Feature.PANEL_INTEGRATION: (
        "اتصال هم‌زمان به ۱۰ پنل "
        "(سنایی، علیرضا، مرزبان، مرزنشین، هیدیفای، ربکا، پاسارگاد، "
        "PasarGuard، یوپنل مرزبان، شاهان)"
    ),
    Feature.COLORED_BUTTONS: "دکمه‌های رنگی",
    # === PATCH_FREE_PLAN_SIMPLIFY_V1 ===
    Feature.GATEWAY_ZARINPAL: "🔐 درگاه زرین‌پال",
    # === PATCH_ZIBAL_GATEWAY_V1 ===
    Feature.GATEWAY_ZIBAL: "🔐 درگاه زیبال",
    Feature.GATEWAY_UNIQUEPAY: "💳 درگاه یونیک‌پی",
    Feature.GATEWAY_APAY: "💳 درگاه آپی (کارت‌به‌کارت آنی)",
    Feature.GATEWAY_ABANGATEWAY: "💳 درگاه آبان گیت‌وی",
    Feature.GATEWAY_STARS: "⭐ پرداخت با استارز تلگرام",
    Feature.GATEWAY_HESABPAY: "🇦🇫 درگاه حساب‌پی",
    Feature.KYC_VERIFY: "🛡 احراز هویت کاربران",
    Feature.TX_LOG_CHANNEL: "📊 کانال لاگ تراکنش‌ها",
    Feature.TRIAL_REPORT_CHANNEL: "🔑 کانال گزارش اکانت تست",
    Feature.ESCROW: "🤝 واسطه‌گری (معامله با واسطه + ساختِ گروه)",
    Feature.METERED_PLAN: "💳 بستهٔ اعتباری (پرداخت به‌ازای مصرف از کیف پول)",
    Feature.BULK_WALLET: "💸 شارژ/کسر همگانی کیف پول",
    Feature.PHONE_VERIFY: "📱 تایید شماره پیش از پرداخت",
    Feature.AUTO_APPROVE_RECEIPT: "🤖 تایید خودکار رسید",
    Feature.WALLET_RECEIPT_BTNS: "🔘 دکمه زیر پیام تأیید واریز",
    Feature.DICE_GAME: "🎲 بازی کن و جایزه بگیر",
    Feature.BOT_BACKUP: "💾 بک‌آپ کامل ربات",
    Feature.ONBOARDING_WIZARD: "🎓 راه‌اندازی حرفه‌ای ورود",
    Feature.AUTO_SUB: "ساخت خودکار ساب",
    Feature.DISCOUNT_CODES: "کدهای تخفیف",
    Feature.GATEWAY_PAYMENT: "درگاه پرداخت آنلاین",
    Feature.REFERRAL: "سیستم زیرمجموعه‌گیری (لینک دعوت)",
    Feature.PRODUCT_SHOP: "فروشگاه محصولات دیجیتال",
    Feature.APPOINTMENT: "رزرو نوبت",
    Feature.POLL: "نظرسنجی و رای‌گیری",
    Feature.AUTO_MESSAGE: "پیام‌های خودکار",
    Feature.REVIEW_SYSTEM: "سیستم نظر و امتیاز",
    Feature.ADVANCED_STATS: "آمار پیشرفته",
    Feature.CUSTOM_MENU: "منوی سفارشی",
    Feature.BROADCAST_PHOTO: "ارسال همگانی با مدیا",
    Feature.USER_TAGS: "برچسب‌گذاری کاربران",
    Feature.SCHEDULED_BROADCAST: "ارسال زمان‌بندی شده",
    Feature.CUSTOM_WELCOME: "ویرایش پیام خوش‌آمد",
    Feature.CHANNEL_POST: "ارسال پیام به کانال با دکمهٔ اینلاینِ دلخواه چسبیده زیر متن",
    Feature.TRUST_CHANNEL: "ارسال رسید سفارش به کانال (کانال اعتمادسازی)",
    Feature.WIREGUARD_CONFIG: "پشتیبانی از کانفیگ وایرگارد",
    # === PATCH_OPENVPN_PLAN_GATE_V1 ===
    Feature.OPENVPN_CONFIG: "پشتیبانی از کانفیگ اوپن‌وی‌پی‌ان (OpenVPN)",
    # === PATCH_COMMUNITY_MGMT_V1 ===
    Feature.COMMUNITY_MGMT: "مدیریتِ گروه/کانال (ضداسپم، خوشامد، فیلترِ کلمات، بن/اخطار)",
    # === PATCH_TENANT_PREMIUM_EMOJI_V1 ===
    Feature.PREMIUM_EMOJI: "افزودن ایموجی پرمیوم",
    # === PATCH_TRUST_TEXT_PRO_ONLY_V1 ===
    Feature.TRUST_CHANNEL_TEXT: "ویرایشِ متنِ پیامِ کانالِ اعتمادسازی (قالبِ دلخواه)",
    Feature.FORCE_JOIN_TEXT: "ویرایشِ متنِ پیامِ عضویتِ اجباری (قالبِ دلخواه)",
    Feature.FORCE_JOIN_COLOR: "🎨 رنگِ دکمه‌هایِ عضویتِ اجباری",
    # === PATCH_PANEL_RELINK_V1 ===
    Feature.PANEL_RELINK: "بازسازیِ لینک‌ها پس از تعویضِ پنل (+ اطلاع به کاربران)",
    # === PATCH_NFT_MARKET_V1 ===
    Feature.NFT_MARKET: "🖼 مینی‌اپ ۲ — مارکتِ خرید و اجارهٔ NFT و یوزرنیم",
    # === PATCH_GUIDE_BTN_COLOR_PRO_V1 ===
    Feature.GUIDE_BTN_COLOR: "رنگِ اختصاصیِ هر دکمهٔ آموزش اتصال",
    # === PATCH_PAYGUIDE_MOVE_FINANCE_PRO_V1 ===
    Feature.PAYMENT_GUIDE: "💰 راهنمای پرداخت و افزایش موجودی",
    # === PATCH_RESELLER_TIERS_V1 ===
    Feature.RESELLER_TIERS: "🏷 نمایندگی خودکار (پله‌های تخفیف بر اساس شارژ/خرید)",
    # === PATCH_WEB_PANEL_V1 ===
    Feature.WEB_PANEL: "🖥 پنل مدیریت تحت وب",
    # === PATCH_SMM_MULTI_PANEL_V1 ===
    Feature.SMM_MULTI_PANEL: "🌐 اتصالِ چند پنلِ SMM هم‌زمان",
    # === PATCH_NODEX_PANEL_V1 ===
    Feature.NODEX_PANEL: "🔷 اتصال به پنل نودکس (GuardCore)",
    Feature.DEGERIS_PANEL: "🟣 اتصال به پنل DEGERIS (ثنایی جدید)",   # PATCH_DEGERIS_PANEL_V1
    Feature.EYLAN_PANEL: "🟠 اتصال به پنل ایلان (EylanPanel)",   # PATCH_EYLAN_PANEL_V1
    Feature.HEIMDALL_PANEL: "🛡 اتصال به پنل HEIMDALL",   # PATCH_HEIMDALL_PANEL_V1
    Feature.RICKPANEL_PANEL: "🟥 اتصال به پنل RickPanel",   # PATCH_RICKPANEL_PANEL_V1
    Feature.PLAN_TABLE_VIEW: "📊 نمایشِ جدولیِ پلن‌ها",   # PATCH_PLAN_TABLE_VIEW_V1
    # === PATCH_VPN_PANEL_MARKET_V1 ===
    Feature.VPN_PANEL_MARKET: "🏷 ثبت و فروشِ پنل",
    # === PATCH_PANEL_YAR_MOVE_V1 ===
    Feature.PANEL_YAR: "🗂 پنل‌یار (مدیریتِ پنل‌های VPN)",
    # === PATCH_SMM_API_ACCESS_V1 ===
    Feature.SMM_API_ACCESS: "🏢 اتصال به پنل/ارائه‌دهندهٔ SMM",
    # === PATCH_TENANT_HOME_STICKER_V1 ===
    Feature.HOME_STICKERS: "🎟 افزودن استیکر روی استارت و دکمه‌های خانه",
    # === PATCH_GROUP_AI_COMMUNITY_MGMT_V1 ===
    Feature.GROUP_AI_ASSISTANT: "🤖 دستیارِ هوشِ مصنوعیِ گروه",
}

# قابلیت‌های پایه که در همهٔ پلن‌ها هست
_BASE = {
    Feature.KEYBOARD_BUTTONS, Feature.INLINE_BUTTONS, Feature.WALLET,
    Feature.MANUAL_INVENTORY, Feature.PURCHASE_FLOW, Feature.CARD_TO_CARD,
    Feature.STOCK_NOTIFY,
    Feature.ADMIN_PANEL, Feature.BOT_STATS, Feature.EDIT_CONFIG,
    Feature.USER_LOOKUP, Feature.QR_DELIVERY, Feature.EDITABLE_BUTTONS,
    Feature.TRUST_CHANNEL, Feature.WIREGUARD_CONFIG,
    Feature.COMMUNITY_MGMT,  # === PATCH_COMMUNITY_MGMT_V1 === از پلنِ رایگان هم فعال
}

# قابلیت‌های پلن رشد (روی پایه اضافه می‌شود)
_GROWTH_EXTRA = {
    # === PATCH_OPENVPN_PLAN_GATE_V1 ===
    Feature.OPENVPN_CONFIG,
    Feature.PANEL_INTEGRATION, Feature.COLORED_BUTTONS,
    Feature.AUTO_SUB, Feature.DISCOUNT_CODES,
    Feature.POLL, Feature.REFERRAL,
    Feature.AUTO_MESSAGE, Feature.REVIEW_SYSTEM,
    Feature.BROADCAST_PHOTO, Feature.CUSTOM_MENU,
    Feature.CUSTOM_WELCOME,
    # === PATCH_SELF_OWNERSHIP_LOCK_V1 ===
    # تعویضِ توکن/انتقالِ مالکیت فقط از پلنِ رشد به بالا؛ رایگان ندارد.
    Feature.SELF_TOKEN_SWAP,
    # === PATCH_PLAN_FEATURE_GATING_V1 ===
    # امتیازگیری با رفرال همان قماشِ Feature.REFERRAL است (که خودش بالاتر
    # همین‌جاست) — پس همان‌جا، از رشد به بالا.
    Feature.REFERRAL_POINTS,
    # === PATCH_SMM_API_ACCESS_V1 ===
    Feature.SMM_API_ACCESS,
    # === PATCH_FREE_PLAN_SIMPLIFY_V1 ===
    # این‌ها قبلاً هیچ گِیتی نداشتند (یعنی رایگان هم می‌دیدشان). حالا از
    # رشد به بالا — پس رشد و حرفه‌ای دقیقاً همان چیزی را دارند که داشتند.
    Feature.GATEWAY_ZARINPAL, Feature.GATEWAY_UNIQUEPAY, Feature.GATEWAY_APAY,
    Feature.GATEWAY_STARS, Feature.GATEWAY_HESABPAY,
    # === PATCH_ABANGATEWAY_V1 === طبقِ خواستهٔ مدیر: رشد و حرفه‌ای.
    Feature.GATEWAY_ABANGATEWAY,
    Feature.KYC_VERIFY, Feature.TX_LOG_CHANNEL, Feature.BULK_WALLET,
    Feature.TRIAL_REPORT_CHANNEL,      # PATCH_TRIAL_REPORT_CHANNEL_V1 — رشد و حرفه‌ای
    Feature.ESCROW,                    # PATCH_ESCROW_V1 — رشد و حرفه‌ای
    Feature.PHONE_VERIFY, Feature.AUTO_APPROVE_RECEIPT,
    Feature.WALLET_RECEIPT_BTNS,
    Feature.DICE_GAME, Feature.BOT_BACKUP, Feature.ONBOARDING_WIZARD,
}

# === PATCH_PARTNER_BOT_V1 ===
# قابلیت‌های «ربات همکار». عمداً از _BASE ارث نمی‌برد: پنلِ همکار پنلِ
# فروشِ کانفیگ نیست، پنلِ بازفروشیِ ربات است. هرچه اینجا نیست، در
# پنلِ همکار دیده نمی‌شود.
_PARTNER = {
    Feature.KEYBOARD_BUTTONS, Feature.INLINE_BUTTONS,
    Feature.COLORED_BUTTONS, Feature.EDITABLE_BUTTONS,
    Feature.WALLET, Feature.CARD_TO_CARD,
    # === PATCH_PARTNER_V15 ===
    # درگاه‌هایِ پرداخت، تا همکار بتواند از کاربرانش با ارز دیجیتال و
    # استارز هم پول بگیرد (نه فقط کارت‌به‌کارت).
    Feature.GATEWAY_PAYMENT,
    Feature.ADMIN_PANEL, Feature.BOT_STATS, Feature.ADVANCED_STATS,
    Feature.USER_LOOKUP, Feature.REFERRAL,
    # === PATCH_PARTNER_SELF_TOKEN_SWAP_V1 ===
    # «تعویضِ توکن / واگذاریِ مالکیت» در «ربات‌های من». تا امروز فقط
    # رشد/حرفه‌ای داشتندش و مالکِ رباتِ همکار برایِ عوض‌کردنِ یک توکن مجبور
    # بود از پشتیبانی درخواست بدهد — در حالی که پنلِ همکار خودش پنلِ
    # بازفروشیِ ربات است و این کار بخشی از کارِ روزمره‌اش است.
    Feature.SELF_TOKEN_SWAP,
    Feature.BROADCAST_PHOTO, Feature.CHANNEL_POST,
    Feature.CUSTOM_WELCOME,
    # === PATCH_FREE_PLAN_SIMPLIFY_V1 ===
    # همکار قبلاً هم این‌ها را می‌دید (چون هیچ گِیتی نداشتند)؛ صریح شد تا
    # اضافه‌شدنِ گِیت چیزی از پنلِ همکار کم نکند.
    Feature.GATEWAY_ZARINPAL, Feature.GATEWAY_UNIQUEPAY, Feature.GATEWAY_APAY,
    Feature.GATEWAY_STARS, Feature.KYC_VERIFY, Feature.TX_LOG_CHANNEL,
    # === PATCH_ABANGATEWAY_V1 ===
    Feature.GATEWAY_ABANGATEWAY,
    Feature.BULK_WALLET, Feature.PHONE_VERIFY,
    Feature.AUTO_APPROVE_RECEIPT, Feature.WALLET_RECEIPT_BTNS,
    Feature.BOT_BACKUP, Feature.ONBOARDING_WIZARD,
    # === PATCH_PARTNER_DISCOUNT_V1 ===
    # کدهای تخفیف برایِ همکار هم باز شد؛ همکار به کاربرانِ خودش ربات
    # می‌فروشد و برایِ کمپین/فروشِ ویژه به کدِ تخفیف نیاز دارد.
    Feature.DISCOUNT_CODES,
    # === PATCH_TENANT_PREMIUM_EMOJI_V1 ===
    Feature.PREMIUM_EMOJI,
    # === PATCH_TRUST_TEXT_PRO_ONLY_V1 ===
    # هابِ «عضویتِ اجباری» برایِ رباتِ همکار هم باز است (از تنظیماتِ
    # عمومی)، پس ویرایشِ متنش را از او نمی‌گیریم.
    Feature.FORCE_JOIN_TEXT,
    # === PATCH_PARTNER_REFERRAL_POINTS_V1 ===
    # باگِ گزارش‌شده: همکار توی رباتش «امتیازگیری با رفرال» رو به‌عنوانِ
    # گیتِ دسترسیِ یه دکمه فعال کرده بود، اما وقتی خواست از «دکمه‌ها و
    # تنظیمات» روشنش کنه، اصلاً همچین گزینه‌ای اونجا نبود — چون
    # Feature.REFERRAL_POINTS توی مجموعهٔ قابلیت‌هایِ پلنِ همکار نبود
    # (فقط Feature.REFERRAL سادهٔ بالا بود). پیامِ خودِ ربات هم می‌گفت
    # «از دکمه‌ها و تنظیمات روشنش کن»، پس این ناسازگاری/بن‌بست واقعی بود.
    Feature.REFERRAL_POINTS,
}

# قابلیت‌های پلن حرفه‌ای (روی رشد اضافه می‌شود)
_PRO_EXTRA = {
    Feature.GATEWAY_PAYMENT,
    Feature.PRODUCT_SHOP, Feature.APPOINTMENT,
    Feature.ADVANCED_STATS, Feature.USER_TAGS,
    Feature.SCHEDULED_BROADCAST,
    Feature.CHANNEL_POST,
    # === PATCH_PLAN_FEATURE_GATING_V1 ===
    # طبقِ خواستهٔ مدیر: این دو، پیشرفته/تزئینی‌اند و باید پلنِ رشد را
    # «خیلی محدودتر» از حرفه‌ای نگه دارند — پس فقط حرفه‌ای.
    Feature.QR_BACKGROUND,
    Feature.COMBO_BUTTON,
    Feature.MULTI_PANEL_INSTANCES,
    # === PATCH_TENANT_PREMIUM_EMOJI_V1 ===
    # طبقِ خواستهٔ مدیر: فقط پلنِ حرفه‌ای (و ربات‌هایِ همکار، جدا در
    # _PARTNER)؛ رایگان و رشد دیگر این قابلیت را نمی‌بینند.
    Feature.PREMIUM_EMOJI,
    # === PATCH_TRUST_TEXT_PRO_ONLY_V1 ===
    Feature.TRUST_CHANNEL_TEXT,
    Feature.FORCE_JOIN_TEXT,
    Feature.FORCE_JOIN_COLOR,   # PATCH_FORCEJOIN_BTN_COLOR_V1
    # === PATCH_PANEL_RELINK_V1 ===
    Feature.PANEL_RELINK,
    # === PATCH_NFT_MARKET_V1 === طبقِ خواستهٔ مدیر: فقط پلنِ حرفه‌ای.
    Feature.NFT_MARKET,
    # === PATCH_GUIDE_BTN_COLOR_PRO_V1 ===
    Feature.GUIDE_BTN_COLOR,
    # === PATCH_PAYGUIDE_MOVE_FINANCE_PRO_V1 ===
    Feature.PAYMENT_GUIDE,
    # === PATCH_RESELLER_TIERS_V1 ===
    # طبقِ خواستهٔ مدیر: نمایندگیِ خودکار فقط پلنِ حرفه‌ای.
    Feature.RESELLER_TIERS,
    # === PATCH_WEB_PANEL_V1 === طبقِ خواستهٔ مدیر: فقط پلنِ حرفه‌ای.
    Feature.WEB_PANEL,
    # === PATCH_TON_DIRECT_PRO_ONLY_V1 ===
    Feature.TON_DIRECT,
    # === PATCH_SMM_MULTI_PANEL_V1 ===
    # طبقِ خواستهٔ مدیر: چندپنلیِ SMM فقط پلنِ حرفه‌ای.
    Feature.SMM_MULTI_PANEL,
    # === PATCH_NODEX_PANEL_V1 === طبقِ خواستهٔ ادمین: فقط پلنِ حرفه‌ای.
    Feature.NODEX_PANEL,
    Feature.DEGERIS_PANEL,   # PATCH_DEGERIS_PANEL_V1
    Feature.EYLAN_PANEL,     # PATCH_EYLAN_PANEL_V1
    Feature.HEIMDALL_PANEL,  # PATCH_HEIMDALL_PANEL_V1
    Feature.RICKPANEL_PANEL,  # PATCH_RICKPANEL_PANEL_V1
    Feature.PLAN_TABLE_VIEW,   # PATCH_PLAN_TABLE_VIEW_V1
    # === PATCH_VPN_PANEL_MARKET_V1 === طبقِ خواستهٔ ادمین: فقط پلنِ حرفه‌ای.
    Feature.VPN_PANEL_MARKET,
    # === PATCH_PANEL_YAR_MOVE_V1 === طبقِ خواستهٔ ادمین: فقط پلنِ حرفه‌ای.
    Feature.PANEL_YAR,
    # === PATCH_ZIBAL_GATEWAY_V1 === طبقِ خواستهٔ ادمین: فقط پلنِ حرفه‌ای.
    Feature.GATEWAY_ZIBAL,
    # === PATCH_TENANT_HOME_STICKER_V1 === طبقِ خواستهٔ ادمین: فقط پلنِ حرفه‌ای.
    Feature.HOME_STICKERS,
    # === PATCH_GROUP_AI_COMMUNITY_MGMT_V1 === طبقِ درخواست: فقط پلنِ حرفه‌ای.
    Feature.GROUP_AI_ASSISTANT,
    # === PATCH_METERED_PLAN_V1 === طبقِ درخواست: فقط پلنِ حرفه‌ای.
    Feature.METERED_PLAN,
}


class Plan(str, Enum):
    FREE = "free"
    GROWTH = "growth"
    PRO = "pro"
    # === PATCH_PARTNER_BOT_V1 ===
    # «ربات همکار»: نوعِ سومِ ربات. مالکش از ما ربات (رشد/حرفه‌ای) به‌صورت
    # عمده می‌خرد و به کاربرانِ خودش می‌فروشد. ربات‌هایی که همکار می‌فروشد،
    # ربات‌های مستأجرِ معمولیِ همین پلتفرم‌اند (plan=growth/pro) — فقط
    # زنجیرهٔ مالکیت‌شان از طریق PartnerBot به همکار وصل است.
    PARTNER = "partner"


# پلن‌بندی پیش‌فرض (بدون override)
PLAN_FEATURES_DEFAULT = {
    Plan.FREE: set(_BASE),
    Plan.GROWTH: _BASE | _GROWTH_EXTRA,
    Plan.PRO: _BASE | _GROWTH_EXTRA | _PRO_EXTRA,
    # === PATCH_PARTNER_BOT_V1 ===
    Plan.PARTNER: set(_PARTNER),
}

# این dict در runtime با override های دیتابیس پُر می‌شود
PLAN_FEATURES: dict[Plan, set] = {
    Plan.FREE: set(PLAN_FEATURES_DEFAULT[Plan.FREE]),
    Plan.GROWTH: set(PLAN_FEATURES_DEFAULT[Plan.GROWTH]),
    Plan.PRO: set(PLAN_FEATURES_DEFAULT[Plan.PRO]),
    # === PATCH_PARTNER_BOT_V1 ===
    Plan.PARTNER: set(PLAN_FEATURES_DEFAULT[Plan.PARTNER]),
}

PLAN_INFO = {
    Plan.FREE:   {"title": "پایه (رایگان)", "price": 0},
    Plan.GROWTH: {"title": "رشد",            "price": 0},
    Plan.PRO:    {"title": "حرفه‌ای",         "price": 0},
    # === PATCH_PARTNER_BOT_V1 ===
    Plan.PARTNER: {"title": "همکار",          "price": 0},
}

# کلید دسترسی‌های ادمین فرعی
SUB_ADMIN_PERMISSIONS = {
    "adm:stats": "📊 آمار",
    "adm:fin": "💳 مالی و رسیدها",
    "adm:orders": "🛒 خریدها",
    "adm:users": "👤 مدیریت کاربران",
    "adm:bots": "🤖 مدیریت ربات‌ها",
    "adm:sub_admins": "👑 مدیریت ادمین فرعی",
    "adm:plan_features": "⚙️ تنظیم قابلیت‌های پلن",
    "adm:partner": "🤝 ربات‌های همکاری",  # === PATCH_PARTNER_SUBADMIN_V1 ===
}


def has_feature(plan: str, feature: Feature) -> bool:
    """آیا این پلن به این قابلیت دسترسی دارد؟ (با در نظر گرفتن override)"""
    try:
        return feature in PLAN_FEATURES[Plan(plan)]
    except (ValueError, KeyError):
        return False


async def reload_plan_features(session) -> None:
    """بارگذاری مجدد override های پلن از دیتابیس."""
    from sqlalchemy import select
    from models import PlanFeatureOverride

    # ابتدا به پیش‌فرض برگرد
    for plan in Plan:
        PLAN_FEATURES[plan] = set(PLAN_FEATURES_DEFAULT[plan])

    # سپس override ها را اعمال کن
    overrides = (await session.scalars(select(PlanFeatureOverride))).all()
    for ov in overrides:
        try:
            plan = Plan(ov.plan)
            feat = Feature(ov.feature_key)
            if ov.enabled:
                PLAN_FEATURES[plan].add(feat)
            else:
                PLAN_FEATURES[plan].discard(feat)
        except (ValueError, KeyError):
            pass


# === PATCH_FILE_LINK_CONVERTER_V1 ===
# سهمیهٔ ذخیره‌سازیِ هر پلن برایِ ربات‌هایِ نوعِ «تبدیل فایل↔لینک»
# (بر حسب مگابایت؛ مجموعِ حجمِ همهٔ فایل‌هایِ آپلودشدهٔ یک ربات).
STORAGE_QUOTA_MB: dict[Plan, int] = {
    Plan.FREE: 500,
    Plan.GROWTH: 5_000,
    Plan.PRO: 20_000,
    Plan.PARTNER: 20_000,  # === PATCH_PARTNER_BOT_V1 ===
}
# حداکثر حجمِ مجازِ هر فایلِ تکی (مستقل از پلن) — برایِ جلوگیری از
# پرشدنِ ناگهانیِ دیسکِ سرور با یک فایلِ حجیم.
MAX_SINGLE_FILE_MB = 500


def upgrade_hint(feature: Feature) -> str:
    """پیامی که موقع لمس قابلیت قفل‌شده نشان داده می‌شود."""
    needed = "رشد"
    if feature in _PRO_EXTRA:
        needed = "حرفه‌ای"
    return f"🔒 این قابلیت در پلن «{needed}» فعال می‌شود.\nبرای استفاده، ربات خود را ارتقا بده."


# === PATCH_PLAN_FEATURES_BY_BOT_TYPE_V1 ===
# قبلاً «امکانات پلن‌ها» فقط یه لیستِ تختِ قابلیت‌ها بود، مستقل از نوعِ
# ربات؛ یعنی مثلاً یه ربات کارت‌به‌کارت هم می‌دید «انبار دستی کانفیگ»
# جزو پلنشه، درصورتی‌که فقط مخصوصِ ربات‌هایِ فروشِ VPNه. اینجا نگاشتِ
# «هر قابلیت مخصوصِ کدوم نوع(هایِ) رباته» تعریف شده تا نمایشِ امکاناتِ
# هر پلن (هم در ربات پلتفرم برایِ کاربر، هم در پنلِ ادمین) بتونه زیرِ هر
# پلن، این‌ها رو تفکیک‌شده به‌تفکیکِ نوعِ ربات نشون بده.

# نوع‌های رباتِ فعال (همون کلیدهایِ FIELDS در platform_bot.py)
BOT_TYPE_LABELS: dict[str, str] = {
    "vpn_sales": "🛡 فروش VPN / کانفیگ",
    "digital_shop": "🛒 فروشگاه دیجیتال",
    "virtual_service": "🤝 خدمات مجازی (SMM)",
    "booking": "📅 نوبت‌دهی",
    "ad_bot": "📢 تبلیغاتی",
    "file_link": "🔗 تبدیل فایل ↔ لینک",
    # === PATCH_PLAN_FEATURE_GATING_V1 ===
    # قبلاً این نوعِ ربات اصلاً اینجا نبود، پس Feature.COMMUNITY_MGMT هیچ‌وقت
    # در این صفحهٔ ادمین (که فقط از رویِ BOT_TYPE_ORDER پیمایش می‌کند)
    # نشان داده نمی‌شد.
    "community_mgmt": "🛡 مدیریت گروه و کانال",
}
BOT_TYPE_ORDER: list[str] = [
    "vpn_sales", "digital_shop", "virtual_service", "booking", "ad_bot", "file_link",
    "community_mgmt",  # === PATCH_PLAN_FEATURE_GATING_V1 ===
]

# مقدار None یعنی قابلیت «عمومی»ه — مخصوصِ نوعِ خاصی از ربات نیست و
# زیرِ همه‌یِ نوع‌ها نشون داده می‌شه. قابلیتی که اینجا نیومده هم پیش‌فرض
# عمومی حساب می‌شه.
FEATURE_BOT_TYPES: dict[Feature, frozenset | None] = {
    # --- مخصوصِ فروشِ VPN / کانفیگ ---
    Feature.MANUAL_INVENTORY: frozenset({"vpn_sales"}),
    Feature.STOCK_NOTIFY: frozenset({"vpn_sales"}),
    Feature.PURCHASE_FLOW: frozenset({"vpn_sales"}),
    Feature.EDIT_CONFIG: frozenset({"vpn_sales"}),
    Feature.QR_DELIVERY: frozenset({"vpn_sales"}),
    # === PATCH_PLAN_FEATURE_GATING_V1 ===
    Feature.QR_BACKGROUND: frozenset({"vpn_sales"}),
    Feature.COMBO_BUTTON: frozenset({"vpn_sales"}),
    Feature.MULTI_PANEL_INSTANCES: frozenset({"vpn_sales"}),
    Feature.PANEL_INTEGRATION: frozenset({"vpn_sales"}),
    # === PATCH_VPN_PANEL_MARKET_V1 ===
    Feature.VPN_PANEL_MARKET: frozenset({"vpn_sales"}),
    # === PATCH_PANEL_YAR_MOVE_V1 ===
    Feature.PANEL_YAR: frozenset({"vpn_sales"}),
    Feature.AUTO_SUB: frozenset({"vpn_sales"}),
    Feature.WIREGUARD_CONFIG: frozenset({"vpn_sales"}),
    # === PATCH_OPENVPN_PLAN_GATE_V1 ===
    Feature.OPENVPN_CONFIG: frozenset({"vpn_sales"}),
    # گزارشِ اکانتِ تستِ VPN فقط برایِ رباتِ فروشِ VPN معنا دارد (PATCH_TRIAL_REPORT_CHANNEL_V1)
    Feature.TRIAL_REPORT_CHANNEL: frozenset({"vpn_sales"}),
    Feature.ESCROW: frozenset({"virtual_service", "escrow_bot"}),     # PATCH_ESCROW_V1 / PATCH_ESCROW_BOT_KIND_V1
    Feature.METERED_PLAN: frozenset({"vpn_sales"}),     # PATCH_METERED_PLAN_V1
    # --- مخصوصِ فروشگاهِ دیجیتال ---
    Feature.PRODUCT_SHOP: frozenset({"digital_shop"}),
    # === PATCH_COMMUNITY_MGMT_V1 ===
    Feature.COMMUNITY_MGMT: frozenset({"community_mgmt"}),
    # === PATCH_GROUP_AI_COMMUNITY_MGMT_V1 ===
    Feature.GROUP_AI_ASSISTANT: frozenset({"community_mgmt"}),
    # --- مخصوصِ نوبت‌دهی ---
    Feature.APPOINTMENT: frozenset({"booking"}),
    # بقیه‌یِ قابلیت‌ها (کیف‌پول، کارت‌به‌کارت، پنل مدیریت، دکمه‌های
    # رنگی، کدِ تخفیف، درگاهِ پرداخت، زیرمجموعه‌گیری، نظرسنجی، پیامِ
    # خودکار، آمار، منویِ سفارشی، ارسالِ همگانی، برچسبِ کاربر، ارسالِ
    # زمان‌بندی‌شده، خوش‌آمدگویی، ارسال پست به کانال و...) عمومی‌ان و
    # برایِ هر پنج نوع ربات یکسان نمایش داده می‌شن.
}


def features_by_bot_type(plan: "Plan", bot_type: str) -> list:
    """قابلیت‌هایِ فعالِ این پلن که برایِ این نوع ربات مرتبط‌اند
    (قابلیت‌هایِ عمومی + قابلیت‌هایِ مخصوصِ همین نوع ربات)، به‌همون
    ترتیبِ تعریف‌شده در Feature."""
    feats = PLAN_FEATURES.get(plan, set())
    result = []
    for feat in Feature:
        if feat not in feats:
            continue
        scope = FEATURE_BOT_TYPES.get(feat)
        if scope is None or bot_type in scope:
            result.append(feat)
    return result
