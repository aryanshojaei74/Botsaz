#!/bin/bash
set -e
cd /opt/botsaz

echo "📦 استخراج همه فایل‌های جدید..."
unzip -o botsaz_patched.zip \
  tenant/__init__.py \
  tenant/vpn_user.py \
  tenant/vpn_admin.py \
  tenant/vpn_panel.py \
  tenant/extras.py \
  platform_bot.py \
  models.py \
  config.py \
  main.py \
  plans.py \
  2>/dev/null || true

echo "🔄 ری‌استارت..."
systemctl restart botsaz
sleep 2

STATUS=$(systemctl is-active botsaz)
if [ "$STATUS" = "active" ]; then
  echo "✅ سرویس بالاست — تغییرات اعمال شد"
else
  echo "❌ مشکل در سرویس:"
  journalctl -u botsaz -n 15 --no-pager
fi
