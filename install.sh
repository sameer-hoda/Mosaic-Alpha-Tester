#!/usr/bin/env bash
# Nembu — one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/sameer-hoda/Nembu-Alpha-Tester/main/install.sh | bash
#
# Installs a release tarball and a bundled Python into ~/.nembu, then starts
# Nembu. Re-run it to update; store/ and .env are never overwritten.
#
# Legacy: if ~/.nembu is missing and ~/.mosaic exists, it is copied with
# permissions preserved and a timestamped backup is created. ~/.mosaic is
# never deleted.
#
# Developers can install from a checkout with:
#   ./install.sh --from-source

set -euo pipefail

REPO="sameer-hoda/Nembu-Alpha-Tester"
# New env, with legacy fallback
NEMBU_HOME="${NEMBU_HOME:-${MOSAIC_HOME:-$HOME/.nembu}}"
LEGACY_HOME="${MOSAIC_HOME:-$HOME/.mosaic}"
APP="$NEMBU_HOME/app"
PYDIR="$NEMBU_HOME/python"
BINDIR="$NEMBU_HOME/bin"
STORE="$NEMBU_HOME/store"
ENVFILE="$NEMBU_HOME/.env"

# Pinned standalone CPython (install_only) from astral-sh/python-build-standalone.
PY_RELEASE="20261026"
PY_VERSION="3.12.16"
PY_URL_ARM="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_RELEASE}/cpython-${PY_VERSION}%2B${PY_RELEASE}-aarch64-apple-darwin-install_only.tar.gz"
PY_SHA_ARM="fbf4afef53c2a6b72bb741bc06d5671a9dfaf87bd84e5aa73ac9b3d0d589f14e"
PY_URL_X86="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_RELEASE}/cpython-${PY_VERSION}%2B${PY_RELEASE}-x86_64-apple-darwin-install_only.tar.gz"
PY_SHA_X86="a962e47df522889eaa7ff0d6a56203f1079f68aabb65ffdbe8455f362bc769f3"

FROM_SOURCE=0
NO_START=0
for arg in "$@"; do
  case "$arg" in
    --from-source) FROM_SOURCE=1 ;;
    --no-start) NO_START=1 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

BOLD='\033[1m'; CYAN='\033[0;36m'; GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
say() { printf "${CYAN}→${NC} %s\n" "$*"; }
ok()  { printf "${GREEN}✓${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}!${NC} %s\n" "$*"; }
die() { printf "${RED}✗${NC} %s\n" "$*" >&2; exit 1; }

OS="$(uname -s)"
ARCH="$(uname -m)"

case "$OS" in
  Darwin)
    case "$ARCH" in
      arm64|aarch64) PLATFORM="darwin-universal"; PY_URL="$PY_URL_ARM"; PY_SHA="$PY_SHA_ARM" ;;
      x86_64)        PLATFORM="darwin-universal"; PY_URL="$PY_URL_X86"; PY_SHA="$PY_SHA_X86" ;;
      *) die "Unsupported Mac architecture: $ARCH" ;;
    esac
    ;;
  Linux)
    case "$ARCH" in
      x86_64|amd64) PLATFORM="linux-amd64" ;;
      arm64|aarch64) PLATFORM="linux-arm64" ;;
      *) die "Unsupported Linux architecture: $ARCH" ;;
    esac
    ;;
  *)
    die "Nembu supports macOS 13+ and Linux. Detected: $OS"
    ;;
esac

sha256_of() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    die "shasum is required"
  fi
}

verify() {
  local file="$1" expected="$2" actual
  actual="$(sha256_of "$file")"
  if [ "$actual" != "$expected" ]; then
    die "Checksum mismatch for $(basename "$file") — download was corrupted or tampered with."
  fi
}

# ── migration from ~/.mosaic to ~/.nembu ────────────────────────────────────
migrate_legacy() {
  local new_home="$1" legacy_home="$2"
  if [ -d "$new_home" ]; then
    return 0
  fi
  if [ ! -d "$legacy_home" ]; then
    return 0
  fi
  say "Migrating existing data from $legacy_home to ${new_home}..."
  local ts
  ts="$(date +%Y%m%d-%H%M%S)"
  local backup_root="$HOME/.nembu-migration-backup-$ts"
  mkdir -p "$backup_root"
  # Preserve permissions with -pR / -a
  if command -v rsync >/dev/null 2>&1; then
    rsync -a "$legacy_home"/ "$backup_root/mosaic-backup/" 2>/dev/null || cp -a "$legacy_home" "$backup_root/"
  else
    cp -a "$legacy_home" "$backup_root/mosaic-backup" 2>/dev/null || cp -pR "$legacy_home" "$backup_root/"
  fi
  mkdir -p "$new_home"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a "$legacy_home"/ "$new_home"/ 2>/dev/null || cp -a "$legacy_home"/. "$new_home"/
  else
    cp -a "$legacy_home"/. "$new_home"/ 2>/dev/null || cp -pR "$legacy_home"/* "$new_home"/ 2>/dev/null || true
    # ensure hidden files like .env copied
    cp -p "$legacy_home/.env" "$new_home/.env" 2>/dev/null || true
  fi
  # Preserve permissions: store 0700, .env 0600, config 0700/0600
  chmod 0700 "$new_home/store" 2>/dev/null || true
  chmod 0600 "$new_home/.env" 2>/dev/null || true
  if [ -d "$new_home/config" ]; then
    chmod 0700 "$new_home/config" 2>/dev/null || true
    chmod 0600 "$new_home/config"/* 2>/dev/null || true
  fi
  ok "Migrated $legacy_home → $new_home (backup at $backup_root)"
  # Also migrate .mosaic-config -> .nembu-config
  if [ ! -d "$HOME/.nembu-config" ] && [ -d "$HOME/.mosaic-config" ]; then
    say "Migrating config from ~/.mosaic-config to ~/.nembu-config…"
    cp -a "$HOME/.mosaic-config" "$HOME/.nembu-config" 2>/dev/null || cp -pR "$HOME/.mosaic-config" "$HOME/.nembu-config"
    chmod 0700 "$HOME/.nembu-config" 2>/dev/null || true
    chmod 0600 "$HOME/.nembu-config"/* 2>/dev/null || true
    # backup
    cp -a "$HOME/.mosaic-config" "$backup_root/mosaic-config-backup" 2>/dev/null || true
    ok "Migrated ~/.mosaic-config → ~/.nembu-config"
  fi
}

migrate_legacy "$NEMBU_HOME" "$LEGACY_HOME"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$NEMBU_HOME" "$STORE" "$BINDIR"

if [ "$FROM_SOURCE" -eq 1 ]; then
  say "Installing from the local checkout…"
  command -v go >/dev/null 2>&1 || die "Go is required for --from-source (brew install go)"
  command -v python3 >/dev/null 2>&1 || die "python3 is required for --from-source"
  mkdir -p "$TMP/app/bin" "$TMP/app/scripts" "$TMP/python/bin"
  ( cd bridge && CGO_ENABLED=1 go build -trimpath -o "$TMP/app/bin/nembu" . )
  # legacy symlink for migration
  ln -sf nembu "$TMP/app/bin/mosaic"
  cp ./*.py "$TMP/app/"
  cp scripts/preflight.py "$TMP/app/scripts/"
  cp VERSION "$TMP/app/"
  # launcher: prefer nembu file, fallback to mosaic file for dev
  if [ -f "nembu" ]; then
    cp nembu "$TMP/app/launcher.sh"
  else
    cp mosaic "$TMP/app/launcher.sh"
  fi
  cp install.sh "$TMP/app/install.sh"
  ln -s "$(command -v python3)" "$TMP/python/bin/python3"
else
  say "Finding the latest release…"
  command -v curl >/dev/null 2>&1 || die "curl is required"
  TAG="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
    | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)"
  [ -n "$TAG" ] || die "No release found yet. Try again after the first release is published."
  VER="${TAG#v}"
  ASSET="nembu-${VER}-${PLATFORM}.tar.gz"
  # Legacy fallback: try nembu asset, then mosaic asset during transition
  BASE="https://github.com/${REPO}/releases/download/${TAG}"

  say "Downloading Nembu ${VER} (${PLATFORM})…"
  if ! curl -fsSL "$BASE/$ASSET" -o "$TMP/app.tar.gz"; then
    # fallback to old naming during migration period
    LEGACY_ASSET="mosaic-${VER}-${PLATFORM}.tar.gz"
    say "Trying legacy asset $LEGACY_ASSET…"
    ASSET="$LEGACY_ASSET"
    curl -fsSL "$BASE/$ASSET" -o "$TMP/app.tar.gz"
  fi
  curl -fsSL "$BASE/checksums.txt" -o "$TMP/checksums.txt"
  EXPECTED="$(grep "  ${ASSET}$" "$TMP/checksums.txt" | awk '{print $1}' | head -1)"
  [ -n "$EXPECTED" ] || die "No checksum for $ASSET"
  verify "$TMP/app.tar.gz" "$EXPECTED"

  mkdir -p "$TMP/app"
  tar -xzf "$TMP/app.tar.gz" -C "$TMP/app"
  # Support both binary names in tarball
  if [ -x "$TMP/app/bin/nembu" ]; then
    :
  elif [ -x "$TMP/app/bin/mosaic" ]; then
    # rename legacy binary to new name, keep symlink
    mv "$TMP/app/bin/mosaic" "$TMP/app/bin/nembu"
    ln -sf nembu "$TMP/app/bin/mosaic"
  else
    die "The release tarball is missing bin/nembu"
  fi

  if [ "$OS" = "Darwin" ]; then
    say "Downloading a private Python ${PY_VERSION}…"
    curl -fsSL "$PY_URL" -o "$TMP/python.tar.gz"
    verify "$TMP/python.tar.gz" "$PY_SHA"
    mkdir -p "$TMP/py"
    tar -xzf "$TMP/python.tar.gz" -C "$TMP/py"
    mv "$TMP/py/python" "$TMP/python"
    [ -x "$TMP/python/bin/python3" ] || die "Bundled Python is missing"
  else
    command -v python3 >/dev/null 2>&1 || die "python3 is required on Linux"
    mkdir -p "$TMP/python/bin"
    ln -s "$(command -v python3)" "$TMP/python/bin/python3"
  fi
fi

say "Installing to ${NEMBU_HOME}…"
rm -rf "$APP" "$PYDIR"
mv "$TMP/app" "$APP"
mv "$TMP/python" "$PYDIR"
chmod +x "$APP/bin/nembu"
# legacy symlink
ln -sf nembu "$APP/bin/mosaic" 2>/dev/null || cp "$APP/bin/nembu" "$APP/bin/mosaic"
# launcher to bin
cp "$APP/launcher.sh" "$BINDIR/nembu"
chmod +x "$BINDIR/nembu"
# legacy wrapper: deprecated mosaic -> nembu
cat > "$BINDIR/mosaic" <<'WRAP'
#!/usr/bin/env bash
# Deprecated: use `nembu` instead. This wrapper exists for migration.
echo "⚠️  'mosaic' is deprecated, use 'nembu' instead." >&2
exec "$(dirname "$0")/nembu" "$@"
WRAP
chmod +x "$BINDIR/mosaic"

if [ ! -f "$ENVFILE" ]; then
  umask 077
  printf 'GEMINI_API_KEY=""\n' > "$ENVFILE"
fi
chmod 600 "$ENVFILE"

add_path_line() {
  local rc="$1"
  touch "$rc"
  grep -qF "$BINDIR" "$rc" && return 0
  printf '\n# Nembu\nexport PATH="%s:$PATH"\n' "$BINDIR" >> "$rc"
}
add_path_line "$HOME/.zprofile"
if [ -f "$HOME/.bash_profile" ]; then
  add_path_line "$HOME/.bash_profile"
fi

if [ "$OS" = "Darwin" ]; then
  command -v xattr >/dev/null 2>&1 && xattr -dr com.apple.quarantine "$NEMBU_HOME" 2>/dev/null || true
fi

ok "Nembu ${VER:-dev} installed"
echo
echo "Next: open a new Terminal window, or run:  ~/.nembu/bin/nembu"
echo

if [ "$NO_START" -eq 0 ]; then
  exec "$BINDIR/nembu"
fi
