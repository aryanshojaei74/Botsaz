#!/bin/bash
# چک سلامت و سرعتِ سرور Botsaz — فقط می‌خونه، چیزی تغییر نمی‌ده.
# اجرا:  bash /opt/botsaz/scripts/server_check.sh > /tmp/check.txt 2>&1 ; cat /tmp/check.txt
cd /opt/botsaz 2>/dev/null || exit 1
H(){ echo; echo "════════ $* ════════"; }

H "1) سرویس‌ها"
systemctl list-units 'botsaz*' --type=service --all --no-legend | awk '{print $1, $3, $4}'
echo "-- restart count / مدت فعال بودن"
for u in $(systemctl list-units 'botsaz*' --type=service --all --no-legend | awk '{print $1}'); do
  echo "$u  NRestarts=$(systemctl show $u -p NRestarts --value)  since=$(systemctl show $u -p ActiveEnterTimestamp --value)"
done

H "2) منابع سرور"
uptime; free -m; df -h / /opt 2>/dev/null | sort -u
echo "-- پرمصرف‌ترین پروسس‌ها"
ps -eo pid,pcpu,pmem,rss,etime,args --sort=-pcpu | head -8 | cut -c1-150

H "3) ارورهای ۶۰ دقیقهٔ اخیر (تجمیع‌شده، به تفکیک سرویس)"
for u in $(systemctl list-units 'botsaz*' --type=service --all --no-legend | awk '{print $1}'); do
  n=$(journalctl -u $u --since "60 min ago" --no-pager 2>/dev/null | grep -ciE "error|exception|traceback")
  echo "$u: $n خط ارور"
done
echo; echo "-- پرتکرارترین پیام‌های ارور (۶ ساعت اخیر)"
journalctl -u 'botsaz*' --since "6 hours ago" --no-pager 2>/dev/null \
 | grep -E "\[ERROR\]|Error|Exception" | sed -E 's/^.{0,40}(botsaz[^ ]*\[[0-9]+\]: )?//; s/[0-9]{5,}/N/g' \
 | cut -c1-160 | sort | uniq -c | sort -rn | head -15

H "4) هشدارهای کندیِ event loop (loopwatch)"
journalctl -u 'botsaz*' --since "6 hours ago" --no-pager 2>/dev/null | grep -iE "loopwatch|event loop|blocked" | tail -8 | cut -c1-220

H "5) دیتابیس"
if command -v psql >/dev/null; then
  DBURL=$(grep -hE "^DATABASE_URL" .env 2>/dev/null | head -1 | cut -d= -f2- | sed 's#postgresql+asyncpg#postgresql#')
  [ -n "$DBURL" ] && {
   psql "$DBURL" -Atc "select 'اتصال‌ها: '||count(*) from pg_stat_activity" 2>&1
   psql "$DBURL" -c "select state,count(*),max(now()-query_start) as longest from pg_stat_activity where datname=current_database() group by 1" 2>&1
   echo "-- کوئری‌های بیش از ۵ ثانیه"
   psql "$DBURL" -c "select pid,now()-query_start d,left(query,100) q from pg_stat_activity where state<>'idle' and now()-query_start>interval '5 s'" 2>&1
   echo "-- جدول‌های بزرگ / seq scan زیاد"
   psql "$DBURL" -c "select relname,seq_scan,idx_scan,n_live_tup from pg_stat_user_tables where seq_scan>1000 and n_live_tup>5000 order by seq_scan*n_live_tup desc limit 8" 2>&1
   echo "-- کندترین کوئری‌ها (اگر pg_stat_statements فعال باشد)"
   psql "$DBURL" -c "select calls,round(mean_exec_time::numeric) ms,round(total_exec_time::numeric) total_ms,left(query,90) q from pg_stat_statements order by total_exec_time desc limit 8" 2>&1 | head -14
  } || echo "DATABASE_URL در .env پیدا نشد"
else echo "psql نصب نیست"; fi

H "6) تست سرعتِ ارتباط با تلگرام"
for i in 1 2 3; do curl -s -o /dev/null -w "api.telegram.org  %{time_total}s\n" https://api.telegram.org; done

H "7) تست سرعتِ پروژه"
[ -x venv/bin/python ] && [ -f scripts/speedcheck.py ] && timeout 100 venv/bin/python scripts/speedcheck.py 2>&1 | tail -25

echo; echo "تمام. خروجی کامل را بفرست."
