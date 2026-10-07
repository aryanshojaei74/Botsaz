#!/usr/bin/env bash
# cdom_sync.sh — دامنه‌هایِ اختصاصیِ تأییدشدهٔ ربات‌ها را رویِ nginx + گواهیِ رایگان (certbot) راه می‌اندازد.
# (PATCH_CDOM_NGINX_V1) — رویِ این سرور وب‌سرور nginx است، نه Caddy؛ پس «on-demand TLS»ِ Caddy که کد
# انتظارش را داشت هیچ‌وقت اجرا نمی‌شد و دامنه‌ها با خطایِ SSL جواب می‌دادند.
#
# برایِ هر دامنهٔ status=verified:
#   ۱) DNS باید به آی‌پیِ همین سرور اشاره کند (وگرنه رد می‌شود تا محدودیتِ Let's Encrypt نخوریم)
#   ۲) کانفیگِ nginx (پروکسی به اپِ ربات‌ها) ساخته می‌شود
#   ۳) گواهیِ SSL با certbot گرفته می‌شود (یک‌بار؛ تمدید خودکارِ خودِ certbot است)
# دامنه‌هایی که دیگر تأییدشده نیستند، کانفیگشان برداشته می‌شود.
#
#   bash deploy/cdom_sync.sh            # همگام‌سازیِ همه
#   bash deploy/cdom_sync.sh --dry-run  # فقط گزارش
set -uo pipefail

APP="${APP:-/opt/botsaz}"
PY="${PY:-$APP/venv/bin/python}"
UPSTREAM="${CDOM_UPSTREAM:-127.0.0.1:8001}"
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1
PREFIX="botsaz-cdom-"
if [ -d /etc/nginx/sites-enabled ]; then
    AVAIL=/etc/nginx/sites-available; ENABLED=/etc/nginx/sites-enabled
else
    AVAIL=/etc/nginx/conf.d; ENABLED=/etc/nginx/conf.d
fi
WEBROOT=/var/www/botsaz-acme
log() { echo "[cdom $(date '+%F %T')] $*"; }

command -v nginx >/dev/null || { log "nginx نصب نیست"; exit 1; }
if ! command -v certbot >/dev/null; then
    if [ $DRY -eq 1 ]; then log "certbot نصب نیست (در اجرایِ واقعی نصب می‌شود)"
    else log "نصبِ certbot…"; DEBIAN_FRONTEND=noninteractive apt-get install -y certbot >/dev/null || { log "نصبِ certbot ناموفق"; exit 1; }
    fi
fi
mkdir -p "$WEBROOT"

SERVER_IPS="$(hostname -I 2>/dev/null) $(curl -s -m 5 -4 ifconfig.me 2>/dev/null) $(curl -s -m 5 -6 ifconfig.me 2>/dev/null)"

mapfile -t DOMAINS < <(cd "$APP" && "$PY" - <<'PYEOF' 2>/dev/null
import asyncio, re
from sqlalchemy import select
from config import Session
from models import BotCustomDomain
ok = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
async def m():
    async with Session() as s:
        for d in (await s.scalars(select(BotCustomDomain.domain).where(BotCustomDomain.status == "verified"))).all():
            d = (d or "").strip().lower()
            if ok.match(d):
                print(d)
asyncio.run(m())
PYEOF
)
log "دامنه‌هایِ تأییدشده: ${DOMAINS[*]:-هیچ}"

write_conf() {   # $1=domain $2=with_ssl(0/1)
    local d="$1" f="$AVAIL/${PREFIX}$1.conf"
    {
        echo "# ساختهٔ خودکارِ deploy/cdom_sync.sh — دستی ویرایش نکن"
        echo "server {"
        echo "    listen 80; listen [::]:80;"
        echo "    server_name $d;"
        echo "    location /.well-known/acme-challenge/ { root $WEBROOT; }"
        if [ "$2" = 1 ]; then
            echo "    location / { return 301 https://\$host\$request_uri; }"
        else
            echo "    location / { proxy_pass http://$UPSTREAM; include /etc/nginx/proxy_params; proxy_set_header X-Forwarded-Host \$host; }"
        fi
        echo "}"
        if [ "$2" = 1 ]; then
            echo "server {"
            echo "    listen 443 ssl; listen [::]:443 ssl;"
            echo "    server_name $d;"
            echo "    ssl_certificate /etc/letsencrypt/live/$d/fullchain.pem;"
            echo "    ssl_certificate_key /etc/letsencrypt/live/$d/privkey.pem;"
            echo "    client_max_body_size 20m;"
            echo "    location / {"
            echo "        proxy_pass http://$UPSTREAM;"
            echo "        proxy_set_header Host \$host;"
            echo "        proxy_set_header X-Real-IP \$remote_addr;"
            echo "        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;"
            echo "        proxy_set_header X-Forwarded-Proto https;"
            echo "        proxy_set_header X-Forwarded-Host \$host;"
            echo "        proxy_read_timeout 120s;"
            echo "    }"
            echo "}"
        fi
    } > "$f"
    [ "$AVAIL" != "$ENABLED" ] && ln -sf "$f" "$ENABLED/${PREFIX}$1.conf"
}

reload_nginx() {
    if nginx -t >/dev/null 2>&1; then systemctl reload nginx; return 0; fi
    log "❌ nginx -t خطا داد:"; nginx -t 2>&1 | tail -5; return 1
}

changed=0
for d in "${DOMAINS[@]}"; do
    ips=$(getent ahosts "$d" | awk '{print $1}' | sort -u | tr '\n' ' ')
    match=0; for ip in $ips; do case " $SERVER_IPS " in *" $ip "*) match=1;; esac; done
    if [ $match -eq 0 ]; then log "⏭ $d — DNS به این سرور نیست ($ips)"; continue; fi
    cert="/etc/letsencrypt/live/$d/fullchain.pem"
    conf="$AVAIL/${PREFIX}$d.conf"
    if [ -f "$cert" ] && grep -q "listen 443" "$conf" 2>/dev/null; then
        log "✅ $d — آماده"; continue
    fi
    if [ $DRY -eq 1 ]; then log "🛠 $d — باید راه‌اندازی شود"; continue; fi
    if [ ! -f "$cert" ]; then
        write_conf "$d" 0
        reload_nginx || { rm -f "$conf" "$ENABLED/${PREFIX}$d.conf"; continue; }
        if ! certbot certonly --webroot -w "$WEBROOT" -d "$d" --non-interactive --agree-tos \
                --register-unsafely-without-email --keep-until-expiring >/tmp/cdom-certbot.log 2>&1; then
            log "❌ $d — گرفتنِ گواهی ناموفق:"; tail -5 /tmp/cdom-certbot.log; continue
        fi
    fi
    write_conf "$d" 1
    if reload_nginx; then log "✅ $d — راه‌اندازی شد (https)"; changed=1
    else write_conf "$d" 0; reload_nginx; fi
done

# کانفیگِ دامنه‌هایی که دیگر تأییدشده نیستند
for f in "$AVAIL/${PREFIX}"*.conf; do
    [ -e "$f" ] || continue
    d=$(basename "$f" .conf); d=${d#"$PREFIX"}
    keep=0; for x in "${DOMAINS[@]}"; do [ "$x" = "$d" ] && keep=1; done
    if [ $keep -eq 0 ]; then
        if [ $DRY -eq 1 ]; then log "🗑 $d — باید برداشته شود"; continue; fi
        rm -f "$f" "$ENABLED/${PREFIX}$d.conf"; log "🗑 $d — برداشته شد"; changed=1
    fi
done
[ $changed -eq 1 ] && [ $DRY -eq 0 ] && reload_nginx >/dev/null
exit 0
