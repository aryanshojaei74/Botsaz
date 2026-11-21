#!/usr/bin/env bash
# پاک‌سازیِ بک‌آپ‌ها / فایل‌هایِ orig-bak / pycache / پچ‌هایِ اجراشده روی خودِ سرور.
# پیش‌فرض: فقط نشون می‌ده چی پاک می‌شه (dry-run). برایِ حذفِ واقعی:
#   bash botsaz-cleanup.sh --apply
#
# استفاده:
#   cd /opt/botsaz          # یا هرجا پروژه هست
#   bash botsaz-cleanup.sh          # فقط نمایش (امن)
#   bash botsaz-cleanup.sh --apply  # حذفِ واقعی

set -euo pipefail

APPLY=0
if [[ "${1:-}" == "--apply" ]]; then
    APPLY=1
fi

echo "پروژه: $(pwd)"
echo "حالت: $([[ $APPLY -eq 1 ]] && echo 'حذفِ واقعی (--apply)' || echo 'فقط نمایش (dry-run)')"
echo

total_before=$(du -sh . 2>/dev/null | cut -f1)

run() {
    # $1 = توضیح, بقیه = دستورِ find
    local desc="$1"; shift
    local matches
    matches=$("$@" 2>/dev/null || true)
    if [[ -z "$matches" ]]; then
        return
    fi
    echo "── $desc ──"
    echo "$matches"
    if [[ $APPLY -eq 1 ]]; then
        echo "$matches" | while IFS= read -r f; do
            [[ -n "$f" ]] && rm -rf -- "$f"
        done
        echo "  ✅ حذف شد"
    fi
    echo
}

# ۱) پوشه‌هایِ بک‌آپِ تاریخ‌دار (.something_YYYYMMDD_HHMMSS_backup)
run "پوشه‌هایِ بک‌آپِ تاریخ‌دار" find . -maxdepth 1 -iname ".*_backup" -type d

# ۲) فایل‌هایِ .bak / .bak2 / .bak_xxx / .orig / .orig_xxx / *_fragment* / *.before_*
run "فایل‌هایِ .bak/.orig/_fragment/.before_ (کلِ درخت)" \
    find . -type f \( -iname "*.bak" -o -iname "*.bak[0-9]" -o -iname "*.bak_*" \
                    -o -iname "*.orig" -o -iname "*.orig_*" \
                    -o -iname "*_fragment*" -o -iname "*.before_*" \)

# ۳) __pycache__ و .pytest_cache
run "پوشه‌هایِ __pycache__" find . -name "__pycache__" -type d
run "پوشه‌هایِ .pytest_cache" find . -name ".pytest_cache" -type d

# ۴) اسکریپت‌هایِ patch_*.py / revert_*.py تویِ ریشه‌یِ پروژه (نه زیرشاخه‌ها)
run "اسکریپت‌هایِ patch_*.py / revert_*.py در ریشه" \
    find . -maxdepth 1 -type f \( -iname "patch_*.py" -o -iname "revert_*.py" \)

echo "──────────────"
if [[ $APPLY -eq 1 ]]; then
    total_after=$(du -sh . 2>/dev/null | cut -f1)
    echo "حجمِ قبل: $total_before   →   حجمِ بعد: $total_after"
else
    echo "این فقط نمایش بود، چیزی حذف نشد. برایِ حذفِ واقعی بزن:"
    echo "  bash $0 --apply"
fi
