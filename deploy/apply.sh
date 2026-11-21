#!/bin/bash
# Botsaz - apply the newest /root/botsaz-delta-<N>-all.zip, restart services in stages.
# Usage:  bash /root/apply.sh
# Overridable for tests: ZIP_DIR (default /root), APP_DIR (default /opt/botsaz)
set -u
ZIP_DIR="${ZIP_DIR:-/root}"
APP_DIR="${APP_DIR:-/opt/botsaz}"

ZIP=$(ls -1 "$ZIP_DIR"/botsaz-delta-*-all.zip 2>/dev/null | sort -t- -k3 -n | tail -1)
if [ -z "$ZIP" ]; then echo "ERROR: no botsaz-delta-*-all.zip found in $ZIP_DIR"; exit 1; fi
N=$(basename "$ZIP" | sed 's/botsaz-delta-\([0-9]*\)-all.zip/\1/')
echo ">>> applying build $N from $ZIP"
cd "$APP_DIR" || exit 1

# backup of the files that will be replaced
unzip -Z1 "$ZIP" | grep -v '^tests/' | tar czf "$ZIP_DIR/backup-before-$N.tgz" -T - 2>/dev/null || true
unzip -o -q "$ZIP" -d "$APP_DIR" || { echo "ERROR: unzip failed"; exit 1; }

# syntax check of every .py file in the zip (except tests) BEFORE any restart
BAD=0
for f in $(unzip -Z1 "$ZIP" | grep '\.py$' | grep -v '^tests/'); do
  python3 -m py_compile "$f" || BAD=1
done
if [ "$BAD" = "1" ]; then
  echo "ERROR: syntax error - restoring backup, NOT restarting"
  tar xzf "$ZIP_DIR/backup-before-$N.tgz" -C "$APP_DIR"
  exit 1
fi
echo "SYNTAX OK"

wait_health() {   # $1=port  $2=max seconds
  local i
  for i in $(seq 1 $(( $2 / 2 ))); do
    # no -f on purpose: ANY http answer (even 404) proves the port is open = uvicorn finished its startup.
    # (main_tenant has no 200 on /health in older builds, which made the old check wait for the full timeout)
    curl -s -o /dev/null -m 2 "http://127.0.0.1:$1/health" && return 0
    sleep 2
  done
  return 1
}

echo ">>> stage 1: botsaz-tenant (all customer bots go through it)"
T0=$(date +%s)
systemctl restart botsaz-tenant
if wait_health 8001 180; then echo "tenant healthy after $(( $(date +%s) - T0 )) s"; else echo "WARNING: tenant not healthy after 180 s"; fi

echo ">>> stage 2: platform + worker"
systemctl restart botsaz-platform botsaz-tenant-worker
wait_health 8000 120 || echo "WARNING: platform slow to come up"

echo ">>> stage 3: per-kind services, ONE AT A TIME (each serves the webhooks of its own bots via nginx)"
for pair in virtual-service:8012 digital-shop:8013 community-mgmt:8015 booking:8014 vpn-test:8011; do
  svc="botsaz-tenant-${pair%%:*}"; port="${pair##*:}"
  T1=$(date +%s)
  systemctl restart "$svc"
  if wait_health "$port" 120; then echo "$svc healthy after $(( $(date +%s) - T1 )) s"; else echo "WARNING: $svc not healthy after 120 s"; fi
done

systemctl is-active botsaz-platform botsaz-tenant botsaz-tenant-worker botsaz-tenant-booking \
  botsaz-tenant-community-mgmt botsaz-tenant-digital-shop botsaz-tenant-virtual-service botsaz-tenant-vpn-test
journalctl -u botsaz-tenant -n 30 --no-pager | grep -iE "error|traceback|syntax" || echo "log clean"
echo ">>> done. /version in a bot should show $N"
