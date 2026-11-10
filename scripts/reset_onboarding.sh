#!/usr/bin/env bash
# reset_onboarding.sh — prepare or repair a first-run onboarding test.
#
# Modes:
#   --full           stop the service, back up store/, .env and config/, remove
#                    them. The next launch asks for the key and a QR, and shows
#                    the full first-run onboarding (including the scope screen).
#   --pairing-only   stop the service, back up, remove the WhatsApp session,
#                    Me-chat state, sync state and pid files. messages.db,
#                    engine.db and .env are kept, so history and the key remain.
#   --test-home      create and print a throwaway NEMBU_HOME. The live install
#                    is not modified; the live service is not stopped.
#
# Every mode requires --yes. Every mode creates
#   ~/.nembu/backups/pre-onboarding-<timestamp>   (mode 0700)
# and prints the exact restore steps. Legacy ~/.mosaic is also checked.
#
# Nothing here sends WhatsApp messages or pairs an account.

set -euo pipefail

NEMBU_HOME="${NEMBU_HOME:-${MOSAIC_HOME:-$HOME/.nembu}}"
MOSAIC_HOME="${MOSAIC_HOME:-$HOME/.mosaic}"
# Use NEMBU_HOME if it exists, else fallback to MOSAIC_HOME for legacy installs
if [ -d "$NEMBU_HOME" ]; then
  ACTIVE_HOME="$NEMBU_HOME"
else
  ACTIVE_HOME="$MOSAIC_HOME"
  if [ -d "$MOSAIC_HOME" ]; then
    NEMBU_HOME="$MOSAIC_HOME"
  fi
fi
STORE="$NEMBU_HOME/store"
ENVFILE="$NEMBU_HOME/.env"
CONFIG_DIR="${NEMBU_CONFIG_DIR:-$NEMBU_HOME/config}"
BACKUPS="$NEMBU_HOME/backups"
TS="$(date +%Y%m%d-%H%M%S)"
BACKUP="$BACKUPS/pre-onboarding-$TS"

BOLD='\033[1m'; CYAN='\033[0;36m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
say()  { printf "${CYAN}→${NC} %s\n" "$*"; }
ok()   { printf "${GREEN}✓${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}!${NC} %s\n" "$*"; }
die()  { printf "${RED}✗${NC} %s\n" "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Usage: scripts/reset_onboarding.sh <mode> --yes

Modes:
  --full           Back up store/, .env and config/, then remove them.
  --pairing-only   Back up, then remove the WhatsApp session and sync state;
                   keep messages.db, engine.db and .env.
  --test-home      Create and print a throwaway NEMBU_HOME; live install untouched.

Required:
  --yes            Confirm the selected mode (all modes, including --test-home).

Examples:
  scripts/reset_onboarding.sh --pairing-only --yes
  scripts/reset_onboarding.sh --full --yes
  scripts/reset_onboarding.sh --test-home --yes

Backups: ~/.nembu/backups/pre-onboarding-<timestamp> (mode 0700)
USAGE
}

MODE=""
ASSUME_YES=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --full|--pairing-only|--test-home)
      [ -z "$MODE" ] || die "choose exactly one mode"
      MODE="${1#--}"
      ;;
    --yes|-y) ASSUME_YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

[ -n "$MODE" ] || { usage; exit 2; }
[ "$ASSUME_YES" -eq 1 ] || {
  warn "refusing to run without --yes"
  usage
  exit 1
}

case "$NEMBU_HOME" in
  ""|"/"|"$HOME") die "refusing unsafe NEMBU_HOME: '$NEMBU_HOME'" ;;
esac

make_backup_dir() {
  mkdir -p "$BACKUPS"
  chmod 700 "$BACKUPS" 2>/dev/null || true
  mkdir -p "$BACKUP"
  chmod 700 "$BACKUP"
}

copy_backup() {
  if [ -d "$STORE" ]; then
    cp -pR "$STORE" "$BACKUP/store"
  else
    warn "no store/ at $STORE (nothing to copy)"
  fi
  if [ -f "$ENVFILE" ]; then
    cp -p "$ENVFILE" "$BACKUP/.env"
    chmod 600 "$BACKUP/.env" 2>/dev/null || true
  else
    warn "no .env at $ENVFILE (nothing to copy)"
  fi
  if [ -d "$CONFIG_DIR" ]; then
    cp -pR "$CONFIG_DIR" "$BACKUP/config"
  else
    warn "no config/ at $CONFIG_DIR (nothing to copy)"
  fi
}

print_restore() {
  local have_store=0 have_env=0 have_config=0
  [ -d "$BACKUP/store" ] && have_store=1
  [ -f "$BACKUP/.env" ] && have_env=1
  [ -d "$BACKUP/config" ] && have_config=1

  cat <<EOF

${BOLD}Backup${NC}: $BACKUP   (mode 0700)
${BOLD}Restore steps${NC} (run only if you need the previous state):
  "$NEMBU_HOME/bin/nembu" stop 2>/dev/null || true
EOF
  if [ "$have_store" -eq 1 ]; then
    cat <<EOF
  rm -rf "$STORE"
  cp -pR "$BACKUP/store" "$STORE"
EOF
  else
    printf '  # (no store/ was in this backup)\n'
  fi
  if [ "$have_env" -eq 1 ]; then
    cat <<EOF
  cp -p "$BACKUP/.env" "$ENVFILE"
  chmod 600 "$ENVFILE"
EOF
  else
    printf '  # (no .env was in this backup)\n'
  fi
  if [ "$have_config" -eq 1 ]; then
    cat <<EOF
  rm -rf "$CONFIG_DIR"
  cp -pR "$BACKUP/config" "$CONFIG_DIR"
EOF
  else
    printf '  # (no config/ was in this backup)\n'
  fi
  cat <<EOF
  "$NEMBU_HOME/bin/nembu" start

EOF
}

stop_service() {
  say "Stopping the Nembu service…"
  if [ -x "$NEMBU_HOME/bin/nembu" ]; then
    "$NEMBU_HOME/bin/nembu" stop >/dev/null 2>&1 || true
  elif [ -x "$NEMBU_HOME/bin/mosaic" ]; then
    "$NEMBU_HOME/bin/mosaic" stop >/dev/null 2>&1 || true
  else
    case "$(uname -s)" in
      Darwin)
        launchctl bootout "gui/$(id -u)/ai.nembu.bridge" >/dev/null 2>&1 || true
        launchctl bootout "gui/$(id -u)/ai.mosaic.bridge" >/dev/null 2>&1 || true
        ;;
      Linux)
        if command -v systemctl >/dev/null 2>&1            && systemctl --user show-environment >/dev/null 2>&1; then
          systemctl --user stop nembu.service >/dev/null 2>&1 || true
          systemctl --user stop mosaic.service >/dev/null 2>&1 || true
        fi
        ;;
    esac
  fi
  for f in "$STORE/worker.pid" "$STORE/bridge.pid"; do
    [ -f "$f" ] || continue
    pid="$(cat "$f" 2>/dev/null || true)"
    if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
    rm -f "$f"
  done
  sleep 1
}

mode_full() {
  stop_service
  say "Backing up store/ and .env…"
  make_backup_dir
  copy_backup
  rm -rf "$STORE" "$CONFIG_DIR"
  rm -f "$ENVFILE"
  if [ "$CONFIG_DIR" != "$HOME/.nembu-config" ]; then rm -rf "$HOME/.nembu-config"; fi
  # Remove the stale LaunchAgent so the next start writes STORE_DIR=~/.nembu/store,
  # not the old ~/.mosaic/store.
  rm -f "$HOME/Library/LaunchAgents/ai.nembu.bridge.plist" \
        "$HOME/Library/LaunchAgents/ai.mosaic.bridge.plist"
  ok "Removed store/, .env, config/ and the stale LaunchAgent. The next launch asks for the Gemini key and a QR, and shows the full first-run onboarding."
  print_restore
}

mode_pairing_only() {
  stop_service
  say "Backing up store/ and .env…"
  make_backup_dir
  copy_backup
  rm -f     "$STORE/whatsapp.db"     "$STORE/whatsapp.db-wal"     "$STORE/whatsapp.db-shm"     "$STORE/whatsapp.db-journal"     "$STORE/mechat.json"     "$STORE/sync_state.json"     "$STORE/bridge.pid"     "$STORE/worker.pid"
  ok "Removed pairing + sync state. Kept messages.db, engine.db, .env and config."
  cat <<EOF

${BOLD}Kept${NC}: $STORE/messages.db, $STORE/engine.db, ${ENVFILE}, $NEMBU_HOME/config/
${BOLD}Next${NC}: open the app and scan a new QR; history stays, Me chat is re-resolved,
      and the welcome is sent again because mechat.json was removed.
EOF
  print_restore
}

mode_test_home() {
  local test_home="${TMPDIR:-/tmp}/nembu-onboarding-test-$TS"
  local real_app="$NEMBU_HOME/app"
  local real_py="$NEMBU_HOME/python/bin/python3"

  make_backup_dir
  cat > "$BACKUP/README.txt" <<EOF
test-home mode: no live files were copied or changed.
The live service was not stopped. This directory exists only because every
reset mode creates a timestamped backup directory.
Timestamp: $TS
NEMBU_HOME (live): $NEMBU_HOME
EOF
  chmod 600 "$BACKUP/README.txt"

  mkdir -p "$test_home/store" "$test_home/config"
  chmod 700 "$test_home" "$test_home/store" "$test_home/config"

  cat <<EOF

${BOLD}Test home${NC}: $test_home   (live install untouched)
No live store, .env, config or service was touched.

Set up this shell:
  export NEMBU_HOME="$test_home"
  export STORE_DIR="$test_home/store"
  export NEMBU_CONFIG_DIR="$test_home/config"
  export PY_SCRIPTS_DIR="$real_app"
  export NEMBU_PYTHON="$real_py"

Run the bridge (terminal A; uses loopback :8090, so stop the live service first
if it is running):
  NEMBU_HOME="$test_home" STORE_DIR="$test_home/store" NEMBU_CONFIG_DIR="$test_home/config" \
    PY_SCRIPTS_DIR="$real_app" NEMBU_PYTHON="$real_py" \
    "$real_app/bin/nembu"

Run the TUI against that bridge (terminal B):
  NEMBU_HOME="$test_home" STORE_DIR="$test_home/store" NEMBU_CONFIG_DIR="$test_home/config" \
    PY_SCRIPTS_DIR="$real_app" NEMBU_PYTHON="$real_py" \
    "$real_app/bin/nembu" tui

Clean up:
  rm -rf "$test_home"

${BOLD}Restore steps${NC}: nothing to restore — no live file was modified.
If you stopped the live service manually for the port, run:
  "$NEMBU_HOME/bin/nembu" start

EOF
  ok "Test home ready; live install untouched."
}

case "$MODE" in
  full) mode_full ;;
  pairing-only) mode_pairing_only ;;
  test-home) mode_test_home ;;
esac
