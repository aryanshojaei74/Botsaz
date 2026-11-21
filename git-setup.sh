#!/usr/bin/env bash
# ============================================================
# git-setup.sh — راه‌اندازیِ git برایِ botsaz و پاک‌سازیِ امن
#
# ترتیبِ کار عمدی است:
#   ۱. git init و کامیتِ وضعیتِ فعلی (شاملِ بکاپ‌ها)
#   ۲. تازه بعدش پاک‌سازی، در یک کامیتِ جدا
# اینطوری هر فایلی که پاک شود در تاریخچه می‌ماند و با یک دستور
# برمی‌گردد. برعکسش (اول پاک، بعد git) برگشت‌ناپذیر بود.
#
# اجرا:
#   cd /opt/botsaz && bash git-setup.sh
# ============================================================
set -euo pipefail

# مسیرِ پروژه — برایِ تست می‌شود override کرد: BOTSAZ_DIR=/tmp/x bash git-setup.sh
cd "${BOTSAZ_DIR:-/opt/botsaz}"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  ✅ %s\n' "$*"; }
warn() { printf '  ⚠️  %s\n' "$*"; }

# ─────────────────────────────────────────────
say "۰) بررسی‌های اولیه"

if [ -d .git ]; then
  warn "پوشهٔ .git از قبل هست — اسکریپت متوقف شد."
  echo "     اگر می‌خواهی از نو شروع کنی: rm -rf .git"
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  warn "git نصب نیست. نصبش کن: apt install git"
  exit 1
fi
ok "git موجود است: $(git --version)"

for f in .gitignore .env.example; do
  [ -f "$f" ] || { warn "$f پیدا نشد — اول کپی‌اش کن."; exit 1; }
done
ok ".gitignore و .env.example سرِ جایشان هستند"

# ─────────────────────────────────────────────
say "۱) راه‌اندازی مخزن"

git init -q
git config user.name  "${GIT_NAME:-botsaz admin}"
git config user.email "${GIT_EMAIL:-admin@botsaz.local}"
git branch -M main
ok "مخزن ساخته شد (شاخهٔ main)"

# ─────────────────────────────────────────────
say "۲) کامیتِ وضعیتِ فعلی — همه‌چیز، حتی بکاپ‌ها"

# موقتاً بکاپ‌ها و پچ‌ها را هم اضافه می‌کنیم تا اگر بعداً چیزی لازم شد
# از تاریخچه درآید. .gitignore فعلی جلویشان را می‌گیرد، پس -f می‌زنیم.
git add -A
git add -f ./*.bak ./*.bak-* ./*.bak2 2>/dev/null || true
git add -f tenant/*.bak tenant/*.bak-* tenant/*.bak2 2>/dev/null || true
git add -f apply_*.py 2>/dev/null || true
git add -f _archive_unused 2>/dev/null || true

if git diff --cached --quiet; then
  warn "چیزی برای کامیت نبود."
else
  git commit -q -m "snapshot: وضعیت پروژه پیش از پاک‌سازی

شاملِ همهٔ فایل‌های بکاپ و اسکریپت‌های پچ، تا اگر بعداً چیزی لازم شد
از همین کامیت قابل بازیابی باشد."
  ok "کامیت اول ثبت شد"
fi

# بررسیِ حیاتی: .env نباید کامیت شده باشد
if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  warn "🔴 .env کامیت شده! فوراً:  git rm --cached .env"
  exit 1
fi
ok ".env کامیت نشده (درست)"

# ─────────────────────────────────────────────
say "۳) پاک‌سازی"

REMOVED=0
rm_it() {
  for p in "$@"; do
    [ -e "$p" ] || continue
    sz=$(du -sh "$p" 2>/dev/null | cut -f1)
    rm -rf "$p"
    printf '  🗑  %-58s %s\n' "$p" "$sz"
    REMOVED=$((REMOVED + 1))
  done
}

echo "── بکاپ‌های دستی (در تاریخچهٔ git هستند) ──"
rm_it ./*.bak ./*.bak2 ./*.bak-* tenant/*.bak tenant/*.bak2 tenant/*.bak-*

echo "── اسکریپت‌های پچِ اعمال‌شده ──"
rm_it apply_partner_wiring.py apply_partner_fix_*.py

echo "── فایلِ سرگردان ──"
# نسخهٔ درستِ این فایل در tenant/ است؛ این یکی از یک cp اشتباه مانده
# و هیچ‌وقت import نمی‌شود (چون from tenant.partner_sales خوانده می‌شود).
if [ -f partner_sales.py ] && [ -f tenant/partner_sales.py ]; then
  rm_it partner_sales.py
fi

echo "── کش و لاگ ──"
rm_it __pycache__ tenant/__pycache__ .pytest_cache
rm_it error.log.1 error.log.2.gz

echo "── آرشیوِ قدیمی ──"
rm_it _archive_unused

find . -name "__pycache__" -not -path "./venv/*" -exec rm -rf {} + 2>/dev/null || true
find . -name "*.pyc" -not -path "./venv/*" -delete 2>/dev/null || true

ok "$REMOVED مورد پاک شد"

# ─────────────────────────────────────────────
say "۴) کامیتِ پاک‌سازی"

git add -A
if git diff --cached --quiet; then
  warn "تغییری برای کامیت نبود."
else
  git commit -q -m "chore: پاک‌سازی بکاپ‌های دستی، پچ‌های اعمال‌شده و کش

از این پس نسخه‌بندی با git انجام می‌شود، پس فایل‌های .bak لازم نیستند.
همه‌شان در کامیت قبلی موجودند."
  ok "کامیت دوم ثبت شد"
fi

# ─────────────────────────────────────────────
say "۵) نتیجه"

echo "  حجم فعلی (بدون venv): $(du -sh --exclude=venv . | cut -f1)"
echo "  کامیت‌ها:"
git --no-pager log --oneline | sed 's/^/    /'
echo
echo "  فایل‌های ردیابی‌شده: $(git ls-files | wc -l)"

cat <<'TXT'

────────────────────────────────────────────
دستورهای روزمره

  git status              چه چیزی عوض شده
  git diff                جزئیاتِ تغییرات
  git add -A && git commit -m "توضیح"
  git log --oneline       تاریخچه

پیش از هر تغییرِ بزرگ:
  git add -A && git commit -m "پیش از X"

اگر خراب شد:
  git checkout -- models.py     یک فایل را برگردان
  git reset --hard HEAD         همه‌چیز به آخرین کامیت

بازیابیِ یک بکاپِ پاک‌شده:
  git log --oneline                          هَشِ کامیت اول را پیدا کن
  git show <hash>:models.py.bak > /tmp/x.py

────────────────────────────────────────────
TXT
