#!/usr/bin/env bash
# ============================================================
# cleanup_project.sh — پاکسازی فایل‌های اضافهٔ botsaz
# هیچ چیزی حذف نمی‌شود؛ همه به پوشهٔ قرنطینه منتقل می‌شوند.
# اجرا از ریشهٔ پروژه:
#   bash cleanup_project.sh          # فقط گزارش (dry-run)
#   bash cleanup_project.sh --apply  # انتقال به قرنطینه
# ============================================================
set -uo pipefail

APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1

TRASH="_trash_$(date +%Y%m%d_%H%M%S)"
TOTAL=0
COUNT=0

move() {
  local p="$1"
  [[ -e "$p" ]] || return 0
  local sz; sz=$(du -sk "$p" 2>/dev/null | cut -f1)
  TOTAL=$((TOTAL + sz)); COUNT=$((COUNT + 1))
  if [[ $APPLY -eq 1 ]]; then
    mkdir -p "$TRASH/$(dirname "$p")"
    mv "$p" "$TRASH/$(dirname "$p")/" 2>/dev/null
  else
    echo "  $p"
  fi
}

echo "### ۱) پوشه‌های بکاپِ مخفی (.xxx_backup)"
for d in .[a-z_]*backup* ._backup; do [[ -e "$d" ]] && move "$d"; done

echo "### ۲) فایل‌های بکاپ (.bak / .prepatch / .before_*)"
while IFS= read -r -d '' f; do move "$f"; done < <(
  find . -path ./"$TRASH" -prune -o \
    \( -name '*.bak' -o -name '*.bak_*' -o -name '*.bak-*' -o -name '*.backup' \
       -o -name '*.prepatch*' -o -name '*.before_*' -o -name '*.broken_unused' \
       -o -name '*.before-fix' \) -print0 )

echo "### ۳) __pycache__"
while IFS= read -r -d '' d; do move "$d"; done < <(find . -name '__pycache__' -type d -print0)

echo "### ۴) لاگ‌ها"
for f in error.log error.log.1 error.log.2.gz; do move "$f"; done

echo "### ۵) اسکریپت‌های یک‌بارمصرفِ پچ/فیکس/ولیدیت/دیباگ"
for f in patch_*.py fix_*.py validate_*.py debug_*.py apply_patches*.py \
         *_patch.py check_arvan.py check_partner_stock.py \
         audit_main_keyboard_layout.py migrate_admin_permissions_column.py \
         test_panel_create_account.py test_partner_loop.py test_zarinpal.py; do
  move "$f"
done

echo "### ۶) متفرقه (زیپ، دیتابیس بکاپ، env قدیمی حاوی توکن)"
move botsaz_ton_direct_patch.zip
move botsaz.db.bak
move .env.before_token_20260819_235936
move backup_restore.py          # کپی تکراری از tenant/backup_restore.py
move tenant_experimental

echo
if [[ $APPLY -eq 1 ]]; then
  echo "✅ $COUNT مورد منتقل شد به: $TRASH  (~$((TOTAL/1024)) MB)"
  echo "بعد از تست سلامت پروژه:  rm -rf $TRASH"
else
  echo "پیش‌نمایش: $COUNT مورد، ~$((TOTAL/1024)) MB"
  echo "برای اجرا:  bash cleanup_project.sh --apply"
fi
