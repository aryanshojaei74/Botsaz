#!/usr/bin/env bash
set -euo pipefail

HOST="${1:-}"
if [ -z "$HOST" ]; then
  echo "usage: bash mts_game_setup.sh <miniapp-host>   e.g. mohem.supraplus.ir"
  exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_DIR=/opt/mts-arena
ENV_FILE=/etc/mts-arena.env
CONF=/etc/nginx/conf.d/supraplus-miniapps.conf
GAME_MAP_DIR=/etc/nginx/miniapp-game.d
PORT_HOST=3101

command -v docker >/dev/null || { echo "docker not found"; exit 1; }
[ -f "$CONF" ] || { echo "$CONF not found"; exit 1; }

printf "Bot token (hidden): "
read -rs TOKEN
echo
if ! echo "$TOKEN" | grep -Eq '^[0-9]{6,}:[A-Za-z0-9_-]{30,}$'; then
  echo "token format is not valid"
  exit 1
fi

mkdir -p "$APP_DIR"
cp "$HERE/mts_arena/mts-arena-server.js" "$HERE/mts_arena/package.json" "$APP_DIR/"
( umask 077; printf 'BOT_TOKEN=%s\nPORT=3000\n' "$TOKEN" > "$ENV_FILE" )
chmod 600 "$ENV_FILE"
unset TOKEN

docker rm -f mts-arena >/dev/null 2>&1 || true
docker run -d --name mts-arena --restart unless-stopped \
  --env-file "$ENV_FILE" \
  -p 127.0.0.1:${PORT_HOST}:3000 \
  --memory 256m --cpus 0.5 --pids-limit 128 \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --user 10001:10001 \
  -v "$APP_DIR":/app:ro -w /app \
  node:20-alpine node mts-arena-server.js >/dev/null

for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS "http://127.0.0.1:${PORT_HOST}/healthz" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS "http://127.0.0.1:${PORT_HOST}/healthz" || { echo; echo "game server did not start:"; docker logs mts-arena | tail -20; exit 1; }
echo

mkdir -p "$GAME_MAP_DIR"
cp -a "$CONF" "$CONF.bak-mts"
python3 "$HERE/mts_nginx_patch.py" "$CONF"
printf '%s 127.0.0.1:%s;\n' "$HOST" "$PORT_HOST" > "$GAME_MAP_DIR/$HOST.map"

if nginx -t; then
  systemctl reload nginx
  echo "nginx reloaded"
else
  cp -a "$CONF.bak-mts" "$CONF"
  rm -f "$GAME_MAP_DIR/$HOST.map"
  nginx -t || true
  echo "nginx config rejected; restored previous config"
  exit 1
fi

echo "--- check via nginx ---"
curl -ksS --resolve "$HOST:8443:127.0.0.1" "https://$HOST:8443/healthz"
echo
echo "done"
