#!/usr/bin/env bash
# errcheck.sh — خلاصهٔ خطاهایِ بات‌ساز برایِ بررسی (فقط می‌خواند؛ چیزی را تغییر نمی‌دهد)
# اجرا:  bash /opt/botsaz/errcheck.sh        ← خروجی در /tmp/botsaz_report.txt هم ذخیره می‌شود
APP="${APP:-/opt/botsaz}"
HOURS="${HOURS:-24}"
OUT="${OUT:-/tmp/botsaz_report.txt}"
LOG="$APP/error.log"

{
echo "════ گزارشِ بات‌ساز — $(date '+%F %T') — بازهٔ ${HOURS} ساعت ════"

echo
echo "── ۱) وضعیتِ سرویس‌ها ──"
if command -v systemctl >/dev/null 2>&1; then
  systemctl list-units 'botsaz*' --type=service --no-legend --no-pager 2>/dev/null \
    | awk '{printf "%-48s %s/%s\n",$1,$3,$4}'
else
  echo "(systemctl در دسترس نیست)"
fi

echo
echo "── ۲) خطاهایِ تکراری error.log (۳۰ موردِ پرتکرار) ──"
if [ -f "$LOG" ]; then
  python3 - "$LOG" "$HOURS" <<'PY'
import re, sys, collections, datetime
path, hours = sys.argv[1], float(sys.argv[2])
cut = datetime.datetime.now() - datetime.timedelta(hours=hours)
head = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[(\w+)\] ([\w.]+): (.*)$')
ev, cur = [], None
for line in open(path, encoding='utf-8', errors='replace'):
    m = head.match(line.rstrip('\n'))
    if m:
        cur = {'t': m.group(1), 'lvl': m.group(2), 'mod': m.group(3), 'msg': m.group(4), 'last': ''}
        ev.append(cur)
    elif cur and line.strip():
        cur['last'] = line.strip()
cnt, lastt = collections.Counter(), {}
for e in ev:
    try:
        if datetime.datetime.strptime(e['t'], '%Y-%m-%d %H:%M:%S') < cut:
            continue
    except ValueError:
        continue
    if 'slow update' in e['msg'] or 'پنلِ کند' in e['msg']:
        continue          # کندی‌ها در بخشِ جدا
    msg = re.sub(r'\d{4,}', 'N', e['msg'])[:90]
    key = (e['lvl'], e['mod'], msg, re.sub(r'\d{4,}', 'N', e['last'])[:110])
    cnt[key] += 1
    lastt[key] = e['t']
if not cnt:
    print("بدونِ خطا در این بازه ✅")
for (lvl, mod, msg, last), n in cnt.most_common(30):
    print(f"{n:>5}×  [{lvl}] {mod}: {msg}")
    if last:
        print(f"        ↳ {last}   (آخرین: {lastt[(lvl, mod, msg, last)]})")
PY
else
  echo "فایلِ $LOG پیدا نشد"
fi

echo
echo "── ۳) کندترین دکمه‌ها ──"
if [ -f "$APP/slow_action_report.py" ] && [ -f "$LOG" ]; then
  grep "slow update" "$LOG" | python3 "$APP/slow_action_report.py" 2>/dev/null | head -28
else
  echo "(گزارشِ کندی در دسترس نیست)"
fi

echo
echo "── ۴) آخرین خطاهایِ سرویس‌ها (journal، ۴۰ خط) ──"
if command -v journalctl >/dev/null 2>&1; then
  journalctl -u 'botsaz*' --since "${HOURS} hours ago" -p err --no-pager -q 2>/dev/null | tail -40
else
  echo "(journalctl در دسترس نیست)"
fi
} 2>&1 | tee "$OUT"

echo
echo "✅ گزارش در $OUT ذخیره شد — محتوایش (یا خودِ فایل) را برایِ من بفرست."
