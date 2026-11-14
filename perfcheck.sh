#!/usr/bin/env bash
# perfcheck.sh — فقط خواندنی؛ هیچ چیزی تغییر نمی‌دهد. خروجی را کامل بفرست.
# اجرا:  cd /opt/botsaz && source venv/bin/activate && bash perfcheck.sh 2>&1 | tee perf_out.txt
cd "$(dirname "$0")"
H() { echo; echo "════════ $1 ════════"; }
PSQL="sudo -u postgres psql -P pager=off -d botsaz -X"

H "A) بار سرور"
uptime; nproc; free -m
vmstat 1 5
echo "--- پرمصرف‌ترین پردازش‌ها"; ps -eo pid,pcpu,pmem,etime,args --sort=-pcpu | head -8 | cut -c1-150

H "B) سرویس‌ها و ری‌استارت"
for u in botsaz-tenant botsaz-platform botsaz-main postgresql nginx; do
  systemctl show "$u" -p ActiveState -p NRestarts -p ActiveEnterTimestamp 2>/dev/null | tr '\n' ' '; echo " ← $u"
done
systemctl list-units 'botsaz*' --no-legend 2>/dev/null | cut -c1-110
echo "--- صفِ اتصالِ پورت ۸۰۰۱ (Recv-Q بالا = رویدادلوپ گیر کرده)"
ss -ltn 2>/dev/null | grep -E ':(8001|80[0-9]{2})\b|State'
echo "--- تعداد اتصالِ برقرار به ۸۰۰۱:"; ss -tn state established '( sport = :8001 )' 2>/dev/null | wc -l

H "C) Postgres"
$PSQL -c "select state, count(*) from pg_stat_activity where datname='botsaz' group by 1 order by 2 desc;" 2>&1
$PSQL -c "show max_connections;" 2>&1
$PSQL -c "select pid, now()-xact_start as xact_age, now()-query_start as q_age, state, left(query,90) q from pg_stat_activity where datname='botsaz' and state<>'idle' and pid<>pg_backend_pid() order by xact_start nulls last limit 10;" 2>&1
$PSQL -c "select count(*) as blocked from pg_stat_activity where wait_event_type='Lock';" 2>&1
$PSQL -c "select relname, n_live_tup, n_dead_tup, seq_scan, idx_scan from pg_stat_user_tables order by seq_scan desc limit 8;" 2>&1
$PSQL -c "select pg_size_pretty(pg_database_size('botsaz')) db_size;" 2>&1

H "D) تلگرام (تأخیر getMe)"
timeout 120 python3 check_telegram_latency.py 2>&1 | tail -15
echo "--- curl به API تلگرام:"
for i in 1 2 3; do curl -s -o /dev/null -w "dns=%{time_namelookup} connect=%{time_connect} tls=%{time_appconnect} total=%{time_total}\n" https://api.telegram.org/ ; done

H "E) webhook همهٔ ربات‌ها"
timeout 300 python3 webhook_check.py --hours 3 2>&1 | tail -40

H "F) آپدیت‌های کند (۶ ساعت اخیر)"
timeout 120 python3 slow_action_report.py 2>&1 | head -40
echo "--- تعداد 'slow update' در هر ساعت:"
journalctl -u botsaz-tenant --since "-12 hours" --no-pager 2>/dev/null | grep -F "slow update" | awk '{print $3}' | cut -d: -f1 | sort | uniq -c | tail -14
echo "--- ۱۵ کندترین:"
journalctl -u botsaz-tenant --since "-12 hours" --no-pager 2>/dev/null | grep -F "slow update" | sed -E 's/.*slow update/slow update/' | sort -t, -k3 -r | awk -F'[,)] *' '{print}' | grep -oE 'kind=[a-z_]+, [0-9.]+s\) → .{0,70}' | sort -t, -k2 -rn | head -15
echo "--- خطاهایِ شبکه/تایم‌اوت/۴۲۹ (۱۲ ساعت):"
journalctl -u botsaz-tenant --since "-12 hours" --no-pager 2>/dev/null | grep -ciE "timeout|timed out" | sed 's/^/timeout: /'
journalctl -u botsaz-tenant --since "-12 hours" --no-pager 2>/dev/null | grep -ciE "retry after|too many requests|429" | sed 's/^/429: /'
journalctl -u botsaz-tenant --since "-12 hours" --no-pager 2>/dev/null | grep -ciE "QueuePool|too many connections|pool" | sed 's/^/db-pool: /'

H "G) nginx"
for f in /var/log/nginx/error.log; do
  [ -r "$f" ] || { echo "$f قابل‌خواندن نیست (با sudo اجرا کن)"; continue; }
  echo "upstream timed out (امروز): $(grep -c 'upstream timed out' $f)"
  echo "connect() failed:           $(grep -c 'connect() failed' $f)"
  echo "worker_connections:         $(grep -c 'worker_connections' $f)"
  tail -5 "$f" | cut -c1-200
done
A=/var/log/nginx/access.log
if [ -r "$A" ]; then
  echo "--- کدهای وضعیت /webhook (۵۰۰۰ خط آخر):"
  tail -5000 "$A" | grep -F "/webhook/" | awk '{print $9}' | sort | uniq -c | sort -rn | head
  echo "--- اگر log_format شامل request_time نباشد، پایین خالی است:"
  tail -5000 "$A" | grep -F "/webhook/" | grep -oE ' rt=[0-9.]+| [0-9]\.[0-9]{3}$' | sort -rn | head -3
fi

H "H) سایت/مستندات (زمانِ پاسخ)"
for u in "${PERF_SITES:-https://google.com}"; do :; done
if [ -f .env ]; then B=$(grep -E '^WEBHOOK_BASE=' .env | cut -d= -f2- | tr -d '"'); fi
[ -n "$B" ] && for i in 1 2 3; do curl -s -o /dev/null -w "$B → %{http_code} total=%{time_total}s\n" "$B/"; done

H "I) رویدادلوپِ main_tenant (py-spy، اختیاری)"
PID=$(systemctl show botsaz-tenant -p MainPID --value 2>/dev/null)
if command -v py-spy >/dev/null && [ -n "$PID" ] && [ "$PID" != 0 ]; then
  for i in 1 2 3; do sudo py-spy dump --pid "$PID" 2>&1 | head -25; echo ---; sleep 1; done
else
  echo "py-spy نصب نیست؛ نیازی نیست. (اختیاری: pip install py-spy)"
fi
echo; echo "پایان. فایل perf_out.txt را بفرست."
