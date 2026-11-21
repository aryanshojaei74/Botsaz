#!/usr/bin/env bash
set -euo pipefail

ZIP="${1:-}"
ROOT="${BOTSAZ_ROOT:-/opt/botsaz}"
SYSCTL="${SYSTEMCTL:-systemctl}"
KEEP="${BOTSAZ_BACKUP_DIR:-/root/botsaz-apply-$(date +%F-%H%M%S)}"

if [ -z "$ZIP" ] || [ ! -f "$ZIP" ]; then
  echo "usage: bash apply_delta.sh /path/to/botsaz-delta-NNN.zip"
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
unzip -q "$ZIP" -d "$TMP"

CHANGED=()
while IFS= read -r -d '' f; do
  rel="${f#"$TMP"/}"
  if [ ! -f "$ROOT/$rel" ] || ! cmp -s "$f" "$ROOT/$rel"; then
    CHANGED+=("$rel")
  fi
done < <(find "$TMP" -type f -print0)

if [ "${#CHANGED[@]}" -eq 0 ]; then
  echo "no changes: everything in this zip is already installed"
  exit 0
fi

ALL_TENANT="botsaz-tenant botsaz-tenant-worker botsaz-tenant-virtual-service botsaz-tenant-digital-shop botsaz-tenant-booking botsaz-tenant-community-mgmt botsaz-tenant-vpn-test"
PLATFORM="botsaz-platform botsaz-platform-worker"
EVERYTHING="$PLATFORM $ALL_TENANT botsaz-booking-worker botsazvip"

declare -A SVC=()
add() { for s in $1; do SVC["$s"]=1; done; }

only_build_id() {
  [ "$1" = "tenant/__init__.py" ] && [ -f "$ROOT/$1" ] &&
    diff -q <(grep -v '^BUILD_ID = ' "$ROOT/$1") <(grep -v '^BUILD_ID = ' "$TMP/$1") >/dev/null
}

for rel in "${CHANGED[@]}"; do
  if only_build_id "$rel"; then continue; fi
  case "$rel" in
    tests/*|scripts/*|deploy/*|docs/*|*.md|*.txt|assets/*|tenant/assets/*) ;;
    tenant/bots/virtual_service/*) add "botsaz-tenant-virtual-service" ;;
    tenant/bots/vpn/*) add "botsaz-tenant botsaz-tenant-vpn-test" ;;
    tenant/bots/digital_shop/*) add "botsaz-tenant-digital-shop" ;;
    tenant/bots/booking/*) add "botsaz-tenant-booking botsaz-booking-worker" ;;
    tenant/bots/community_mgmt/*) add "botsaz-tenant-community-mgmt" ;;
    tenant/*|main_tenant.py|worker_tenant.py|tenant_*.py) add "$ALL_TENANT" ;;
    worker_booking.py) add "botsaz-booking-worker" ;;
    worker_platform.py) add "botsaz-platform-worker" ;;
    main_platform.py|platform_*.py|fake_join_guard.py) add "$PLATFORM" ;;
    hosted_miniapp*.py|hosting_*.py) add "botsazvip $PLATFORM" ;;
    models.py|config.py|plans.py|wallet_crypto.py|logging_setup.py|premium_emoji*.py) add "$EVERYTHING" ;;
    *.py) add "$EVERYTHING" ;;
  esac
done

mkdir -p "$KEEP"
for rel in "${CHANGED[@]}"; do
  if [ -f "$ROOT/$rel" ]; then
    mkdir -p "$KEEP/$(dirname "$rel")"
    cp -a "$ROOT/$rel" "$KEEP/$rel"
  fi
  mkdir -p "$ROOT/$(dirname "$rel")"
  cp -f "$TMP/$rel" "$ROOT/$rel"
done

echo "changed files: ${#CHANGED[@]}"
printf '  %s\n' "${CHANGED[@]}"
echo "previous versions saved in: $KEEP"

LIST=()
for s in "${!SVC[@]}"; do
  if "$SYSCTL" cat "$s" >/dev/null 2>&1; then LIST+=("$s"); fi
done

if [ "${#LIST[@]}" -eq 0 ]; then
  echo "no service needs a restart"
  exit 0
fi

IFS=$'\n' LIST=($(printf '%s\n' "${LIST[@]}" | sort)); unset IFS
echo "restarting: ${LIST[*]}"
"$SYSCTL" restart "${LIST[@]}"
sleep "${BOTSAZ_WAIT:-15}"
for s in "${LIST[@]}"; do
  printf '%-45s %s\n' "$s" "$("$SYSCTL" is-active "$s" || true)"
done
