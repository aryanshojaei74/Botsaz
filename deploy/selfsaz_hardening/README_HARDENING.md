# سخت‌سازی SelfSaz (سرویس `/opt/selfsaz`)

این بسته روی کدِ **موجود** شما پچ می‌شود (نیازی به جایگزینیِ فایل‌های بزرگ نیست) و فقط روی سرورِ سلف‌ساز اجرا می‌شود.

## چه چیزهایی درست می‌شود
| # | مشکل | راه‌حل |
|---|------|--------|
| 1 | «رمزنگاری» سشن‌ها XOR بود | Fernet (AES + HMAC) با کلید مشتق‌شده (PBKDF2)؛ مهاجرت خودکارِ رکوردهای قبلی با بکاپ |
| 2 | سشن و API hash داخل env هر worker می‌رفت (قابل‌خواندن از `/proc`) و **کلیدهای سرویس هم به worker می‌رسید** | سشن از stdin (pipe) می‌رسد؛ env کارگر whitelist است |
| 3 | **داده‌های مشتری‌ها مشترک بود**: `insults.txt`، `enemies.txt`، `mmauto_reactions.json`، `saved_photos/`، `backups/` و `data/general_auto_reply.json` برای همهٔ حساب‌ها یکی بود (پاسخ عمومیِ یک مشتری از حساب مشتریِ دیگر ارسال می‌شد) | هر حساب پوشهٔ کاریِ جدا (`data/accounts/<owner_id>`)؛ پاسخ عمومی per-user |
| 4 | راه‌اندازیِ مجدد worker هر ۱۰ ثانیه بدون backoff | backoff نمایی (۱۵ث → ۱۵دقیقه)؛ بعد از ۶ کرشِ پیاپی وضعیت `error` و توقف تا اتصال دوبارهٔ کاربر |
| 5 | سقف منابع نبود | سقف تعداد worker (`SELFSAZ_MAX_WORKERS`=80)، سقف RSS هر worker (`SELFSAZ_WORKER_RSS_MB`=700)، سقف‌های systemd |
| 6 | لاگ‌ها بی‌پایان رشد می‌کرد | چرخش خودکار (۸MB، یک نسخهٔ `.1`) |
| 7 | صفحهٔ اتصال: بدون rate-limit، کلاینت‌های نیمه‌باز نشت می‌کرد، متن خطای خام نمایش داده می‌شد | ۵ درخواست کد و ۸ تلاش ورود در ۱۰ دقیقه برای هر لینک؛ سقف ۲۰ نشست هم‌زمان با پاکسازی؛ پیام‌های خطای امن |
| 8 | ابزار اسپم و پاسخ فحش‌دهندهٔ خودکار | **پیش‌فرض غیرفعال** (`SELFSAZ_ENABLE_SPAM=1` / `SELFSAZ_ENABLE_INSULTS=1` در `.env` برای فعال‌سازی) |
| 9 | وابستگی‌ها پین نبود | `requirements.lock.txt` از نسخه‌های نصب‌شدهٔ فعلی |
| 10 | `/health` از سلامت supervisor خبر نمی‌داد | فیلد `supervisor_age` (ثانیه از آخرین heartbeat) |

## نصب
```bash
# روی سرور سلف‌ساز، با root
cd /root && unzip -o selfsaz-hardening.zip && cd selfsaz_hardening
bash install_hardening.sh
```
اسکریپت: پچ را اول بدون تغییر بررسی می‌کند (اگر لنگری پیدا نشود **هیچ فایلی تغییر نمی‌کند**) → بکاپ کامل در `/root/selfsaz-backup-*.tar.gz` → توقف سرویس‌ها → اعمال → مهاجرت سشن‌ها → راه‌اندازی → `health`.

## بعد از نصب (۲ دقیقه)
```bash
curl -s http://127.0.0.1:8791/health          # supervisor_age باید زیر ~۲۰ باشد
tail -n 20 /opt/selfsaz/data/worker_logs/account_*.log
ls /opt/selfsaz/data/accounts/                  # یک پوشه برای هر حساب
```
حتماً یک حساب تست را باز کنید و با پنل (`پنل`) چند دستور بزنید.

## ⚠️ تغییر رفتارِ قابل‌توجه
- لیست دشمن/فحش/ریکشن و پاسخ عمومیِ قبلی که بین همه مشترک بود به `data/legacy_shared/` منتقل می‌شود (حذف نمی‌شود). هر مشتری از صفر شروع می‌کند.
- اسپم و پاسخ فحش خاموش است؛ بخواهید روشن شود در `/opt/selfsaz/.env` بگذارید `SELFSAZ_ENABLE_SPAM=1` و `systemctl restart selfsaz-worker`.
- workerها یک‌بار ری‌استارت می‌شوند (چند ثانیه قطعی).

## چرخش کلیدها (به‌خاطر نشت آرشیو)
1. **API key** (بین سرویس و بات): مقدار جدید در `/opt/selfsaz/.env` (`SELFSaz_API_KEY`) و همان مقدار در `.env` بات اصلی (`SELFSaz_API_KEY`) → ری‌استارت هر دو.
2. **SESSION_KEY** (رمز سشن‌ها): کلید فعلی را در فایلی ۰۶۰۰ بگذارید، کلید جدید را در `.env` عوض کنید و:
```bash
cd /opt/selfsaz && install -m 600 /dev/null /root/oldkey && echo -n 'کلید-قبلی' > /root/oldkey
SELFSAZ_OLD_KEY_FILE=/root/oldkey .venv/bin/python migrate_vault.py /opt/selfsaz
shred -u /root/oldkey && systemctl restart selfsaz selfsaz-worker
```
3. اگر شک دارید دیتابیس نشت کرده: از مشتری‌ها بخواهید در تلگرام *Settings → Devices* نشستِ سلف را ببندند و دوباره وصل شوند.
4. `API_ID/API_HASH` تلگرام را هم از my.telegram.org بچرخانید.

## بازگردانی
```bash
systemctl stop selfsaz selfsaz-worker
tar xzf /root/selfsaz-backup-<زمان>.tar.gz -C /opt
systemctl start selfsaz selfsaz-worker
```
(هر فایلِ پچ‌شده‌ی `*.bak-hardening` هم کنارش هست.)

## هنوز پوشش داده نشده
- کنترل‌ها و صف دستورها هنوز با فایل JSON کار می‌کنند (برای مقیاس بالا باید به جدول DB بروند).
- مسیر `/v1/accounts/<عدد>` هم با owner_id و هم با account_id معنی می‌شود (بات فعلی به آن وابسته است؛ بدون تغییرِ بات نباید جدا شود).
- worker پروسهٔ جدا برای هر حساب است؛ برای صدها حساب بهتر است به worker چندحسابیِ async برود.
