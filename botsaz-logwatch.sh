#!/usr/bin/env bash
# botsaz-logwatch.sh — دیدنِ زنده یا خلاصه‌سازیِ خطاهای error.log بات‌ساز.
#
# استفاده:
#   ./botsaz-logwatch.sh                → زنده (live): هر خطای تازه رو همون
#                                          لحظه با traceback کامل نشون می‌ده
#   ./botsaz-logwatch.sh --summary       → خلاصهٔ همهٔ فایل‌های لاگِ فعلی
#                                          (error.log + error.log.1 + .gz):
#                                          پرتکرارترین باگ‌ها اول
#   ./botsaz-logwatch.sh --summary 2h    → فقط خطاهای ۲ ساعتِ اخیر (h/m/d)
#   ./botsaz-logwatch.sh --since "18:40" → زنده، ولی از یه ساعتِ مشخص جلوتر
#
# مسیر پیش‌فرض: /opt/botsaz  — اگه پروژه‌ات جای دیگه‌ست:
#   BOTSAZ_DIR=/path/to/project ./botsaz-logwatch.sh

set -euo pipefail
DIR="${BOTSAZ_DIR:-/opt/botsaz}"
LOG="$DIR/error.log"
export LC_ALL=C.UTF-8 2>/dev/null || true

RED='\033[1;31m'; YEL='\033[1;33m'; CYN='\033[1;36m'; GRN='\033[1;32m'; NC='\033[0m'

if [[ ! -f "$LOG" ]]; then
    echo "فایل پیدا نشد: $LOG  (اگه پروژه جای دیگه‌ست: BOTSAZ_DIR=/path ./botsaz-logwatch.sh)"
    exit 1
fi

# ───────────────────────── حالتِ خلاصه ─────────────────────────
if [[ "${1:-}" == "--summary" ]]; then
    WINDOW="${2:-}"
    FILES=("$LOG")
    [[ -f "$LOG.1" ]] && FILES+=("$LOG.1")
    for gz in "$DIR"/error.log.*.gz; do
        [[ -f "$gz" ]] && FILES+=("$gz")
    done

    CAT_CMD="cat"
    TMP=$(mktemp)
    for f in "${FILES[@]}"; do
        if [[ "$f" == *.gz ]]; then zcat "$f" >> "$TMP"; else cat "$f" >> "$TMP"; fi
    done

    if [[ -n "$WINDOW" ]]; then
        SINCE_EPOCH=$(date -d "-$WINDOW" +%s 2>/dev/null || date -v-"$WINDOW" +%s)
        awk -v since="$SINCE_EPOCH" '
            /^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} \[/ {
                cmd = "date -d \"" $1" "$2"\" +%s 2>/dev/null"
                cmd | getline ts; close(cmd)
                keep = (ts+0 >= since)
            }
            keep { print }
        ' "$TMP" > "$TMP.f"
        mv "$TMP.f" "$TMP"
    fi

    echo -e "${CYN}=== خلاصهٔ خطاها (پرتکرارترین‌ها اول) ===${NC}"
    echo
    # هر بلاک با یه خط زمان‌دار [ERROR]/[WARNING] شروع می‌شه؛ امضای هر باگ رو از
    # آخرین خطِ "File ..., line N, in func" پیش از پیامِ نهاییِ Exception می‌گیریم
    # (اونجاست که واقعاً خطا رخ داده، نه جایی که فقط دوباره raise شده).
    awk '
        BEGIN { sig=""; msg=""; last=""; capture=0 }
        /^[0-9]{4}-[0-9]{2}-[0-9]{2} .* \[(ERROR|CRITICAL)\]/ {
            if (sig != "") { print sig " ::: " msg; }
            sig=""; msg=$0; sub(/^[0-9-]+ [0-9:]+ \[[A-Z]+\] /, "", msg)
            last=""; capture=1
            next
        }
        capture && /^  File "/ { last = $0; next }
        capture && (/Error:/ || /Exception:/) && sig == "" && last != "" {
            errline = $0; sub(/^[A-Za-z0-9_.]+: */, "", errline)
            sig = last " -> " substr(errline, 1, 120)
            capture = 0
            next
        }
        END { if (sig != "") print sig " ::: " msg }
    ' "$TMP" | sort | uniq -c | sort -rn | head -25 | \
    sed -E 's/^ *([0-9]+) (.*)$/\1\t\2/' | \
    awk -F'\t' '{ printf "\033[1;33m× %-4s\033[0m %s\n", $1, $2 }'

    echo
    echo -e "${CYN}(برای دیدنِ کاملِ یه موردِ خاص: grep -A 25 \"بخشی از پیام بالا\" $LOG)${NC}"
    rm -f "$TMP"
    exit 0
fi

# ───────────────────────── حالتِ زنده ─────────────────────────
SINCE_ARG=""
if [[ "${1:-}" == "--since" && -n "${2:-}" ]]; then
    SINCE_ARG="${2}"
fi

echo -e "${CYN}در حالِ دیدنِ زندهٔ $LOG ... (خروج: Ctrl+C)${NC}"
echo -e "${CYN}هر خطای تازه با traceback کاملش همین‌جا نشون داده می‌شه.${NC}"
echo

tail -n0 -F "$LOG" 2>/dev/null | while IFS= read -r line; do
    if [[ "$line" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}:[0-9]{2}\ \[(ERROR|CRITICAL)\] ]]; then
        echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo -e "${RED}$line${NC}"
    elif [[ "$line" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}:[0-9]{2}\ \[WARNING\] ]]; then
        echo -e "${YEL}$line${NC}"
    elif [[ "$line" =~ ^\ \ File\ \" ]]; then
        echo -e "${GRN}$line${NC}"
    elif [[ "$line" =~ (Error|Exception): ]]; then
        echo -e "${RED}${line}${NC}"
        echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    else
        echo "$line"
    fi
done
