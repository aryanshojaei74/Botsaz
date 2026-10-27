#!/usr/bin/env bash
# سخت‌سازی SelfSaz روی سرور (/opt/selfsaz). اجرا با root:  bash install_hardening.sh
# قبل از هر تغییر بکاپ کامل گرفته می‌شود؛ اگر پچ روی کد شما اعمال نشود هیچ فایلی تغییر نمی‌کند.
set -euo pipefail
ROOT="${SELFSAZ_ROOT:-/opt/selfsaz}"
KIT="$(cd "$(dirname "$0")" && pwd)"
TS="$(date +%Y%m%d-%H%M%S)"
BACKUP="/root/selfsaz-backup-$TS.tar.gz"
PY="$ROOT/.venv/bin/python"

[ "$(id -u)" = 0 ] || { echo "❌ با root اجرا کنید"; exit 1; }
[ -f "$ROOT/selfsaz_service/app.py" ] || { echo "❌ $ROOT/selfsaz_service/app.py پیدا نشد"; exit 1; }
[ -x "$PY" ] || { echo "❌ venv در $ROOT/.venv پیدا نشد"; exit 1; }
grep -qE '^(export )?SELFSaz_SESSION_KEY=.{16,}' "$ROOT/.env" || { echo "❌ SELFSaz_SESSION_KEY (حداقل ۱۶ کاراکتر) در $ROOT/.env نیست"; exit 1; }

echo "1) بررسی پچ (بدون تغییر)…"
"$PY" "$KIT/apply_patches.py" "$ROOT" --dry-run

echo "2) توقف سرویس‌ها (برای بکاپِ سازگار؛ چند دقیقه بیشتر نمی‌شود)"
restart_old() { systemctl start selfsaz.service selfsaz-worker.service 2>/dev/null || true; }
trap 'echo "⚠️ خطا قبل از اعمال تغییر؛ سرویس‌ها با کد قبلی دوباره روشن می‌شوند"; restart_old' ERR
systemctl stop selfsaz-worker.service 2>/dev/null || true
systemctl stop selfsaz.service 2>/dev/null || true
sleep 2

echo "3) بکاپ کامل → $BACKUP"
tar czf "$BACKUP" --exclude="$(basename "$ROOT")/.venv" -C "$(dirname "$ROOT")" "$(basename "$ROOT")"
chmod 600 "$BACKUP"

rollback_hint() { echo; echo "⚠️ خطا رخ داد. بازگردانی:  systemctl stop selfsaz selfsaz-worker; tar xzf $BACKUP -C $(dirname "$ROOT"); systemctl start selfsaz selfsaz-worker"; }
trap rollback_hint ERR   # از اینجا به بعد فایل‌ها تغییر می‌کنند؛ بازگردانی دستی با بکاپ

echo "4) وابستگی‌ها (cryptography) و قفل نسخه‌ها"
"$ROOT/.venv/bin/pip" install -q cryptography
"$ROOT/.venv/bin/pip" freeze > "$ROOT/requirements.lock.txt"

echo "5) اعمال پچ‌ها"
"$PY" "$KIT/apply_patches.py" "$ROOT"

echo "6) کپی فایل‌های جدید"
install -m 0644 "$KIT/files/selfsaz_service/vault.py"      "$ROOT/selfsaz_service/vault.py"
install -m 0644 "$KIT/files/selfsaz_service/guards.py"     "$ROOT/selfsaz_service/guards.py"
install -m 0644 "$KIT/files/selfsaz_service/clockname.py"  "$ROOT/selfsaz_service/clockname.py"
install -m 0644 "$KIT/files/selfsaz_service/autoreply.py"  "$ROOT/selfsaz_service/autoreply.py"
[ -f "$ROOT/selfsaz_service/supervisor.py" ] && cp -p "$ROOT/selfsaz_service/supervisor.py" "$ROOT/selfsaz_service/supervisor.py.bak-hardening"
install -m 0644 "$KIT/files/selfsaz_service/supervisor.py" "$ROOT/selfsaz_service/supervisor.py"
install -m 0755 "$KIT/files/migrate_vault.py"              "$ROOT/migrate_vault.py"
touch "$ROOT/selfsaz_service/__init__.py"

echo "7) مهاجرت رمزنگاری سشن‌ها"
"$PY" "$ROOT/migrate_vault.py" "$ROOT"

echo "8) فایل‌های مشترکِ قدیمی (بین همهٔ مشتری‌ها) → data/legacy_shared"
mkdir -p "$ROOT/data/legacy_shared"
for f in insults.txt enemies.txt mmauto_reactions.json saved_photos backups data/general_auto_reply.json; do
  [ -e "$ROOT/$f" ] && mv "$ROOT/$f" "$ROOT/data/legacy_shared/" && echo "   منتقل شد: $f" || true
done

echo "9) دسترسی‌ها"
id selfsaz >/dev/null 2>&1 && chown -R selfsaz:selfsaz "$ROOT/data" "$ROOT/selfsaz_service" "$ROOT/requirements.lock.txt" || true
chmod 700 "$ROOT/data"
chmod 600 "$ROOT"/.env* "$ROOT"/data/*.sqlite3* 2>/dev/null || true

echo "10) سرویس‌ها"
install -m 0644 "$KIT/files/selfsaz.service"        /etc/systemd/system/selfsaz.service
install -m 0644 "$KIT/files/selfsaz-worker.service" /etc/systemd/system/selfsaz-worker.service
systemctl daemon-reload
systemctl start selfsaz.service
sleep 3
systemctl start selfsaz-worker.service
sleep 5
trap - ERR
curl -fsS http://127.0.0.1:8791/health && echo
systemctl --no-pager --lines=0 status selfsaz selfsaz-worker | grep -E "Active|Loaded" || true
echo
echo "✅ تمام. بکاپ: $BACKUP"
echo "یادآوری: SELFSaz_API_KEY و SELFSaz_SESSION_KEY را بچرخانید (README_HARDENING.md بخش «چرخش کلیدها»)."
