#!/usr/bin/env bash
# cdom_sync.sh — دامنه‌هایِ اختصاصیِ تأییدشدهٔ ربات‌ها را رویِ nginx + گواهیِ رایگان (certbot) راه می‌اندازد.
# (PATCH_CDOM_NGINX_V2) رویِ این سرور وب‌سرور nginx است (نه Caddy) و پورتِ 443 را یک بلوکِ
# `stream` با ssl_preread بر اساسِ نامِ دامنه پخش می‌کند: دامنه‌هایِ شناخته‌شده → 127.0.0.1:8443
# (سایت‌ها)، بقیه → پیش‌فرض (VPN). پس هر دامنه باید
#   ۱) گواهی بگیرد (certbot، از راهِ پورتِ 80)
#   ۲) سرورِ https‌اش رویِ همان پورتِ داخلی (127.0.0.1:8443) گوش بدهد
#   ۳) به map‌ِ stream اضافه شود (فایلِ جدا: /etc/nginx/botsaz-cdom-sni.map که یک‌بار include می‌شود)
# اگر stream وجود نداشت، سرورِ https مستقیم رویِ 443 گوش می‌دهد.
#
#   bash deploy/cdom_sync.sh            # همگام‌سازیِ همه
#   bash deploy/cdom_sync.sh --dry-run  # فقط گزارش
set -uo pipefail

APP="${APP:-/opt/botsaz}"
PY="${PY:-$APP/venv/bin/python}"
UPSTREAM="${CDOM_UPSTREAM:-127.0.0.1:8001}"
INTERNAL_TLS="${CDOM_INTERNAL_TLS:-127.0.0.1:8443}"
SNI_MAP=/etc/nginx/botsaz-cdom-sni.map
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

# فایلی که map‌ِ SNIِ stream در آن است (اگر هست)
STREAM_FILE=$(grep -lE 'map[[:space:]]+\$ssl_preread_server_name' /etc/nginx/*.conf /etc/nginx/conf.d/*.conf 2>/dev/null | head -1)
if [ -n "$STREAM_FILE" ]; then
    HTTPS_LISTEN="listen $INTERNAL_TLS ssl;"
    log "حالتِ stream/SNI ($STREAM_FILE) — https داخلی رویِ $INTERNAL_TLS"
else
    HTTPS_LISTEN="listen 443 ssl; listen [::]:443 ssl;"
fi

SERVER_IPS="$(hostname -I 2>/dev/null) $(curl -s -m 5 -4 ifconfig.me 2>/dev/null) $(curl -s -m 5 -6 ifconfig.me 2>/dev/null)"

# فقط خط‌هایِ «CDOM <domain>» — چاپ‌هایِ موقعِ بالا آمدنِ اپ نادیده گرفته می‌شوند
mapfile -t DOMAINS < <(cd "$APP" && "$PY" - <<'PYEOF' 2>/dev/null | sed -n 's/^CDOM //p'
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
                print("CDOM", d)
asyncio.run(m())
PYEOF
)
log "دامنه‌هایِ تأییدشده: ${DOMAINS[*]:-هیچ}"

render_conf() {   # $1=domain $2=with_ssl(0/1) → stdout
    local d="$1"
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
    [ "$2" = 1 ] || return 0
    echo "server {"
    echo "    $HTTPS_LISTEN"
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
}

changed=0
put_file() {   # $1=path, محتوا از stdin؛ فقط اگر فرق داشت می‌نویسد
    local tmp; tmp=$(mktemp)
    cat > "$tmp"
    if [ -f "$1" ] && cmp -s "$tmp" "$1"; then rm -f "$tmp"; return 1; fi
    mv "$tmp" "$1"; chmod 644 "$1"; changed=1; return 0
}
enable_conf() { [ "$AVAIL" != "$ENABLED" ] && ln -sf "$AVAIL/${PREFIX}$1.conf" "$ENABLED/${PREFIX}$1.conf"; return 0; }
reload_nginx() {
    if nginx -t >/dev/null 2>&1; then systemctl reload nginx; return 0; fi
    log "❌ nginx -t خطا داد:"; nginx -t 2>&1 | tail -5; return 1
}

LIVE=()
for d in "${DOMAINS[@]}"; do
    ips=$(getent ahosts "$d" | awk '{print $1}' | sort -u | tr '\n' ' ')
    match=0; for ip in $ips; do case " $SERVER_IPS " in *" $ip "*) match=1;; esac; done
    if [ $match -eq 0 ]; then log "⏭ $d — DNS به این سرور نیست (${ips:-بدونِ رکورد})"; continue; fi
    cert="/etc/letsencrypt/live/$d/fullchain.pem"
    if [ $DRY -eq 1 ]; then
        [ -f "$cert" ] && log "✅ $d — گواهی دارد" || log "🛠 $d — گواهی باید گرفته شود"
        LIVE+=("$d"); continue
    fi
    if [ ! -f "$cert" ]; then
        render_conf "$d" 0 | put_file "$AVAIL/${PREFIX}$d.conf"; enable_conf "$d"
        reload_nginx || { rm -f "$AVAIL/${PREFIX}$d.conf" "$ENABLED/${PREFIX}$d.conf"; continue; }
        if ! certbot certonly --webroot -w "$WEBROOT" -d "$d" --non-interactive --agree-tos \
                --register-unsafely-without-email --keep-until-expiring >/tmp/cdom-certbot.log 2>&1; then
            log "❌ $d — گرفتنِ گواهی ناموفق:"; tail -5 /tmp/cdom-certbot.log; continue
        fi
        log "🔐 $d — گواهی گرفته شد"
    fi
    render_conf "$d" 1 | put_file "$AVAIL/${PREFIX}$d.conf"; enable_conf "$d"
    LIVE+=("$d")
done

# map‌ِ SNI: دامنه‌هایِ زنده → پورتِ داخلیِ سایت‌ها (+ یک‌بار include در map‌ِ stream)
if [ -n "$STREAM_FILE" ]; then
    if [ $DRY -eq 1 ]; then
        grep -q "$SNI_MAP" "$STREAM_FILE" || log "🛠 include در $STREAM_FILE باید اضافه شود"
        log "map: ${LIVE[*]:-هیچ}"
    else
        { echo "# ساختهٔ خودکارِ deploy/cdom_sync.sh — دامنه‌هایِ اختصاصیِ ربات‌ها"
          for d in "${LIVE[@]}"; do echo "    $d $INTERNAL_TLS;"; done; } | put_file "$SNI_MAP"
        if ! grep -q "$SNI_MAP" "$STREAM_FILE"; then
            cp -a "$STREAM_FILE" "$STREAM_FILE.bak-cdom-$(date +%s)"
            sed -i -E "/map[[:space:]]+\\\$ssl_preread_server_name/a\\        include $SNI_MAP;" "$STREAM_FILE"
            changed=1; log "➕ include به $STREAM_FILE اضافه شد (نسخهٔ قبلی: .bak-cdom-*)"
        fi
    fi
fi

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

if [ $DRY -eq 0 ] && [ $changed -eq 1 ]; then
    if reload_nginx; then log "✅ nginx بارگذاری شد — زنده: ${LIVE[*]:-هیچ}"
    else log "⚠️ nginx بارگذاری نشد؛ تغییراتِ بالا را بررسی کن"; fi
fi
exit 0
