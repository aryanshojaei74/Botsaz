#!/usr/bin/env bash
# اعمالِ یک زیپِ اصلاحی رویِ سرور — با بکاپ، چکِ سینتکس و برگشتِ خودکار.
#
# استفاده (زیپ را کنارِ همین اسکریپت یا هر جایی گذاشته باشی):
#   bash apply_fix.sh /opt/botsaz/botsaz-fix-04.zip
#
# برگشت به نسخهٔ قبل (اگر بعد از دیپلوی مشکلی دیدی):
#   bash apply_fix.sh --rollback /opt/botsaz/fix_backups/botsaz-fix-04-XXXXXXXX-XXXXXX.tar.gz
set -euo pipefail

APP="${APP:-/opt/botsaz}"
PY="${PY:-$APP/venv/bin/python}"
BK_DIR="$APP/fix_backups"

restart_services() {
    # همهٔ سرویس‌هایِ botsaz که الان فعال‌اند (پلتفرم + همهٔ ورکرهایِ تنانت)
    mapfile -t SVCS < <(systemctl list-units --type=service --state=active --no-legend 'botsaz*' \
                        | awk '{print $1}')
    if [ "${#SVCS[@]}" -eq 0 ]; then
        echo "⚠️  سرویسِ فعالی با نامِ botsaz* پیدا نشد؛ دستی ری‌استارت کن."; return
    fi
    echo "🔄 ری‌استارت: ${SVCS[*]}"
    systemctl restart "${SVCS[@]}"
    sleep 8
    local bad=0
    for s in "${SVCS[@]}"; do
        if systemctl is-active --quiet "$s"; then echo "   ✅ $s"; else echo "   ❌ $s"; bad=1; fi
    done
    return $bad
}

if [ "${1:-}" = "--rollback" ]; then
    BK="${2:?مسیرِ فایلِ بکاپ را بده}"
    echo "↩️  برگرداندنِ $BK"
    tar -xzf "$BK" -C "$APP"
    restart_services
    exit $?
fi

ZIP="${1:?مسیرِ زیپ را بده، مثلاً: bash apply_fix.sh /opt/botsaz/botsaz-fix-04.zip}"
[ -f "$ZIP" ] || { echo "❌ فایل پیدا نشد: $ZIP"; exit 1; }
cd "$APP"
mkdir -p "$BK_DIR"
NAME=$(basename "$ZIP" .zip)
TS=$(date +%Y%m%d-%H%M%S)
BK="$BK_DIR/$NAME-$TS.tar.gz"

# ۱) فهرستِ فایل‌هایِ زیپ و بکاپ از نسخهٔ فعلیِ همان‌ها
mapfile -t FILES < <("$PY" -c 'import sys,zipfile; [print(n) for n in zipfile.ZipFile(sys.argv[1]).namelist() if not n.endswith("/")]' "$ZIP")
EXISTING=()
for f in "${FILES[@]}"; do [ -f "$f" ] && EXISTING+=("$f"); done
tar -czf "$BK" "${EXISTING[@]}" 2>/dev/null || tar -czf "$BK" --files-from /dev/null
echo "💾 بکاپ: $BK (${#EXISTING[@]} فایل)"

# ۲) باز کردنِ زیپ
"$PY" -c 'import sys,zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' "$ZIP" "$APP"
echo "📦 ${#FILES[@]} فایل جایگزین شد"

# ۳) چکِ سینتکس؛ اگر خراب بود، برگشت
BADPY=0
for f in "${FILES[@]}"; do
    case "$f" in *.py) "$PY" -m py_compile "$f" || BADPY=1 ;; esac
done
if [ "$BADPY" -ne 0 ]; then
    echo "❌ خطای سینتکس — برگرداندنِ بکاپ و توقف (سرویس‌ها ری‌استارت نشدند)"
    tar -xzf "$BK" -C "$APP"
    exit 1
fi
echo "✅ سینتکس سالم"

# ۴) ری‌استارت و بررسیِ لاگ
LOG_LINES_BEFORE=$(wc -l < "$APP/error.log" 2>/dev/null || echo 0)
if ! restart_services; then
    echo "❌ یک سرویس بالا نیامد. لاگ: journalctl -u <نامِ سرویس> -n 50"
    echo "   برایِ برگشت: bash $0 --rollback $BK"
    exit 1
fi
sleep 5
NEW=$(tail -n +"$((LOG_LINES_BEFORE + 1))" "$APP/error.log" 2>/dev/null \
      | grep -E "ImportError|ModuleNotFoundError|SyntaxError|NameError|AttributeError" | head -5 || true)
if [ -n "$NEW" ]; then
    echo "⚠️  خطایِ تازه در error.log:"; echo "$NEW"
    echo "   برایِ برگشت: bash $0 --rollback $BK"
else
    echo "🎉 دیپلوی تمام شد؛ خطایِ تازه‌ای در error.log نیست."
    echo "   (برگشت در صورتِ نیاز: bash $0 --rollback $BK)"
fi
