#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_DIR=/opt/mts-arena
ENV_FILE=/etc/mts-arena.env
PORT_HOST=3101
PAT='^[0-9]{6,}:[A-Za-z0-9_-]{30,}$'

command -v docker >/dev/null || { echo "docker not found"; exit 1; }
[ -f "$ENV_FILE" ] || { echo "$ENV_FILE not found"; exit 1; }

existing=""
while IFS='=' read -r k v; do
  case "$k" in BOT_TOKEN|BOT_TOKENS) existing="$existing,$v" ;; esac
done < "$ENV_FILE"

tokens=""
for t in $(echo "$existing" | tr ',;' '  '); do
  echo "$t" | grep -Eq "$PAT" && case ",$tokens," in *",$t,"*) ;; *) tokens="$tokens,$t" ;; esac
done
tokens="${tokens#,}"
echo "bots already configured: $(echo "$tokens" | tr ',' '\n' | grep -c . || true)"

while true; do
  printf "Add bot token (hidden, empty to finish): "
  read -rs T
  echo
  [ -z "$T" ] && break
  if ! echo "$T" | grep -Eq "$PAT"; then
    echo "token format is not valid"
    continue
  fi
  case ",$tokens," in *",$T,"*) echo "already added" ;; *) tokens="${tokens:+$tokens,}$T"; echo "added" ;; esac
done
unset T

[ -n "$tokens" ] || { echo "no token configured"; exit 1; }

cp "$HERE/mts_arena/mts-arena-server.js" "$HERE/mts_arena/package.json" "$APP_DIR/"
( umask 077; printf 'BOT_TOKENS=%s\nPORT=3000\n' "$tokens" > "$ENV_FILE" )
chmod 600 "$ENV_FILE"
unset tokens existing

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
docker logs --tail 5 mts-arena
echo "done"
