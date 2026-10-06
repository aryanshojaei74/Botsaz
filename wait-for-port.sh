#!/bin/bash
# منتظر می‌مونه پورت 8000 واقعاً آزاد بشه قبل از بالا اومدنِ نسخه‌ی جدید
PORT=8000
for i in $(seq 1 30); do
    if (exec 3<>/dev/tcp/127.0.0.1/$PORT) 2>/dev/null; then
        exec 3<&- 2>/dev/null
        exec 3>&- 2>/dev/null
        sleep 0.3
    else
        exit 0
    fi
done
exit 0
