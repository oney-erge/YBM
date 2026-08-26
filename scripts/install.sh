#!/usr/bin/env bash
# One-command bootstrap for YBM on Linux/macOS:
#   curl -fsSL https://raw.githubusercontent.com/oney-erge/YBM/main/scripts/install.sh | bash
#
# Git, Python, Node.js, and uv do not need to be preinstalled. This script
# requires Bash, curl, and tar. It downloads the latest release archive, which
# already contains the built admin console, then hands off to ./ybm.sh.
#
# Keep this in step with scripts/install.ps1 - the two have drifted before.
# Both now do the same small job: get the release onto the machine, then hand off
# to the platform's launcher (./ybm.sh here, scripts\ybm.ps1 run there), which
# owns uv, the virtualenv, setup, and starting the stack.
set -euo pipefail

# The pinned uv version lives in ./ybm.sh now, which is what installs it.
RELEASE_URL="https://github.com/oney-erge/YBM/releases/latest/download/YBM-unix.tar.gz"
CHECKSUMS_URL="https://github.com/oney-erge/YBM/releases/latest/download/SHA256SUMS.txt"
INSTALL_DIR="${YBM_INSTALL_DIR:-$HOME/ybm}"

DRY_RUN="${YBM_DRY_RUN:-0}"
VERIFY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --verify) VERIFY=1 ;;
    --no-prompt) : ;;  # accepted for parity; nothing here blocks on input
    --install-dir) shift; INSTALL_DIR="$1" ;;
    -h|--help)
      echo "usage: install.sh [--dry-run] [--verify] [--no-prompt] [--install-dir DIR]"
      exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

log()  { printf '==> %s\n' "$1"; }
info() { printf '    %s\n' "$1"; }
plan() { printf '[dry-run] %s\n' "$1"; }
fail() {
  printf '\nERROR: %s\n' "$1" >&2
  [ $# -gt 1 ] && printf '  %s\n' "$2" >&2
  exit 1
}

download_release() {
  local url=$1 destination=$2
  curl --fail --location --silent --show-error --retry 2 --retry-delay 1 +       --retry-connrefused --output "$destination" --write-out '%{http_code}' "$url"
}

# --- 1. Get the complete release -----------------------------------------
# Source archives intentionally omit the generated admin console. The public
# installer therefore downloads the release archive, not the main branch.
if [ -f "backend/pyproject.toml" ] && [ -f "AGENTS.md" ] && [ -f "scripts/ybm.ps1" ]; then
  REPO_DIR="$(pwd)"
  log "Already inside a YBM checkout"
  info "$REPO_DIR"
elif [ "$DRY_RUN" = "1" ]; then
  REPO_DIR="$INSTALL_DIR"
  plan "would download and extract $RELEASE_URL into $INSTALL_DIR"
else
  log "Downloading the latest YBM release"
  tmp="$(mktemp -d)"
  if ! status="$(download_release "$RELEASE_URL" "$tmp/ybm.tar.gz")"; then
    rm -rf "$tmp"
    fail "download failed" "Check your internet connection and re-run."
  fi
  if [ "$status" = "404" ]; then
    rm -rf "$tmp"
    fail "the latest release archive is not available (HTTP 404)" \
         "Open https://github.com/oney-erge/YBM/releases/latest and check that YBM-unix.tar.gz exists."
  fi
  [ "$status" = "200" ] || { rm -rf "$tmp"; fail "download failed (HTTP $status)" "Check your internet connection and re-run."; }
  log "Verifying the downloaded release"
  if ! checksum_status="$(download_release "$CHECKSUMS_URL" "$tmp/SHA256SUMS.txt")"; then
    rm -rf "$tmp"
    fail "checksum download failed" "Check your internet connection and re-run."
  fi
  [ "$checksum_status" = "200" ] || { rm -rf "$tmp"; fail "checksum download failed (HTTP $checksum_status)" "Open the latest YBM release and report the broken checksum file."; }
  expected="$(awk '$2 == "YBM-unix.tar.gz" || $2 ~ /^YBM-[^[:space:]]+-unix\.tar\.gz$/ { print tolower($1); exit }' "$tmp/SHA256SUMS.txt")"
  [ -n "$expected" ] || { rm -rf "$tmp"; fail "the release checksum file has no Unix archive entry" "Open the latest YBM release and report the broken release."; }
  if command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "$tmp/ybm.tar.gz" | awk '{ print tolower($1) }')"
  elif command -v shasum >/dev/null 2>&1; then
    actual="$(shasum -a 256 "$tmp/ybm.tar.gz" | awk '{ print tolower($1) }')"
  else
    rm -rf "$tmp"
    fail "no SHA256 tool is available" "Install coreutils (sha256sum) or use macOS shasum, then re-run."
  fi
  [ "$actual" = "$expected" ] || { rm -rf "$tmp"; fail "the downloaded release failed its SHA256 check" "Delete the download and try again."; }
  info "release checksum matches"
  tar -xzf "$tmp/ybm.tar.gz" -C "$tmp"
  extracted="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  [ -n "$extracted" ] || { rm -rf "$tmp"; fail "the downloaded release was empty" "Re-run the installer."; }
  mkdir -p "$(dirname "$INSTALL_DIR")"
  if [ -d "$INSTALL_DIR" ]; then
    info "refreshing the existing install and preserving local state"
    cp -R "$extracted"/. "$INSTALL_DIR"/
  else
    mv "$extracted" "$INSTALL_DIR"
  fi
  rm -rf "$tmp"
  REPO_DIR="$INSTALL_DIR"
  info "ready at $INSTALL_DIR"
fi

# --- 2. Hand off to ybm.sh -----------------------------------------------
if [ "$DRY_RUN" = "1" ]; then
  plan "would run: $REPO_DIR/ybm.sh"
  [ "$VERIFY" = "1" ] && plan "would then run: ybm doctor (--verify)"
  echo ""
  echo "Dry run complete - nothing was installed or changed."
  exit 0
fi

cd "$REPO_DIR"
log "Installing dependencies and starting YBM"
# Runtime extras only, because ybm.sh is the consumer path. A contributor who
# wants pytest/ruff/telethon runs the `uv sync --extra test --extra dev` line
# AGENTS.md documents, exactly as on Windows.
bash "$REPO_DIR/ybm.sh" \
  || fail "startup failed" "Run '$REPO_DIR/backend/.venv/bin/ybm doctor' to diagnose. Logs: $REPO_DIR/.agent_control/logs"

YBM_BIN="$REPO_DIR/backend/.venv/bin/ybm"
if [ "$VERIFY" = "1" ]; then
  log "Verifying the install"
  "$YBM_BIN" doctor \
    || fail "post-install verification failed" \
            "The stack installed but doctor reported problems - see the [FAIL] lines above."
  info "verified"
fi

echo ""
log "Pick a model and (optionally) Telegram in the admin console that just opened."
