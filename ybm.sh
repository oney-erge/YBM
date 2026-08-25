#!/usr/bin/env bash
# The one file a non-developer needs on macOS and Linux - the counterpart to
# YBM.bat on Windows. Installs whatever is missing, does nothing when there is
# nothing to do, and opens the console.
#
#   ./ybm.sh              start (installing anything missing first)
#   ./ybm.sh --no-desktop skip the desktop-control extras
#   ./ybm.sh --no-browser do not open the console automatically
#
# Why this exists: Windows had `ybm.ps1 run` as a single idempotent entry
# point, and macOS/Linux had nothing equivalent. The documented path was
# "run scripts/install.sh, then ./backend/.venv/bin/ybm start --open" - two
# commands, one of which only works after the other, and neither of which is
# obvious from a directory listing. The Python CLI cannot fill the gap because
# it lives inside the virtualenv this has to create.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

NO_DESKTOP=0
NO_BROWSER=0
for arg in "$@"; do
  case "$arg" in
    --no-desktop) NO_DESKTOP=1 ;;
    --no-browser) NO_BROWSER=1 ;;
    -h|--help)
      echo "usage: ./ybm.sh [--no-desktop] [--no-browser]"
      exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# Pinned deliberately, and kept in step with scripts/lib/common.ps1's
# $Script:YbmUvVersion. An unpinned
# installer means two machines a week apart get different uv versions.
UV_VERSION="0.9.7"
UV_INSTALLER="https://astral.sh/uv/${UV_VERSION}/install.sh"

log()  { printf '\033[36m==> %s\033[0m\n' "$1"; }
info() { printf '    %s\n' "$1"; }
fail() {
  printf '\n\033[31mERROR: %s\033[0m\n' "$1" >&2
  [ $# -gt 1 ] && printf '  %s\n' "$2" >&2
  exit 1
}

# Resolved by path, never by name alone: a PATH entry written by the uv
# installer is not visible to this already-running shell, so `command -v uv`
# fails on exactly the fresh machine that just installed it.
find_uv() {
  for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    [ -x "$candidate" ] && { printf '%s' "$candidate"; return 0; }
  done
  command -v uv 2>/dev/null || return 1
}

ensure_uv() {
  local uv
  uv="$(find_uv || true)"
  if [ -n "$uv" ]; then printf '%s' "$uv"; return 0; fi
  log "Installing uv ${UV_VERSION} (standalone; no Python needed)" >&2
  curl -LsSf "$UV_INSTALLER" | sh >&2 \
    || fail "could not install uv from $UV_INSTALLER" \
            "Check your internet connection, then try again. uv is the only thing YBM needs to bootstrap."
  uv="$(find_uv || true)"
  [ -n "$uv" ] || fail "uv installed but could not be located" \
                       "Looked in ~/.local/bin and ~/.cargo/bin."
  printf '%s' "$uv"
}

# pyproject.toml as well as uv.lock: a hand-edited pyproject that has not been
# re-locked still changes what `uv sync` resolves, and a fingerprint that
# missed it would leave a stale venv looking up to date.
lock_fingerprint() {
  local files=()
  [ -f backend/pyproject.toml ] && files+=(backend/pyproject.toml)
  [ -f backend/uv.lock ] && files+=(backend/uv.lock)
  [ ${#files[@]} -eq 0 ] && return 1
  if command -v sha256sum >/dev/null 2>&1; then
    cat "${files[@]}" | sha256sum | cut -d' ' -f1
  else
    # macOS ships shasum, not sha256sum.
    cat "${files[@]}" | shasum -a 256 | cut -d' ' -f1
  fi
}

echo ""
printf '\033[36mYBM\033[0m\n'
echo "==========="
echo ""

# A first run downloads uv, a Python runtime, and a few hundred MB of
# dependencies. An unexplained multi-minute pause reads as a hang rather than
# as progress, so say what is happening and roughly how long before it starts.
if [ ! -x "backend/.venv/bin/python" ]; then
  printf '\033[33mFirst run - setting things up.\033[0m\n'
  echo "This downloads Python and YBM's dependencies, and usually takes 2-5 minutes."
  echo "You only wait once; after this YBM starts in a few seconds."
  echo ""
fi

UV="$(ensure_uv)"

# Runtime extras only. Keep this list in step with Get-YbmRuntimeExtraArgs in
# scripts/ybm.ps1: someone double-clicking their way to a running console has
# no use for pytest, ruff, or the Telethon E2E client.
EXTRAS=(--extra voice --extra tray --inexact)
[ "$NO_DESKTOP" = "0" ] && EXTRAS+=(--extra desktop)

VENV_PY="backend/.venv/bin/python"
FINGERPRINT_FILE="backend/.venv/.ybm_sync_fingerprint"
CURRENT_FP="$(lock_fingerprint || true)"
STORED_FP=""
[ -f "$FINGERPRINT_FILE" ] && STORED_FP="$(cat "$FINGERPRINT_FILE")"

if [ -x "$VENV_PY" ] && [ -n "$CURRENT_FP" ] && [ "$CURRENT_FP" = "$STORED_FP" ]; then
  info "[1/3] Dependencies up to date - skipping sync."
else
  log "[1/3] Installing dependencies (the long part on a first run)"
  ( cd backend && "$UV" sync "${EXTRAS[@]}" ) \
    || fail "dependency install failed" "See the message above."
  [ -n "$CURRENT_FP" ] && printf '%s' "$CURRENT_FP" > "$FINGERPRINT_FILE"
fi

YBM_BIN="$HERE/backend/.venv/bin/ybm"
[ -x "$YBM_BIN" ] || fail "the virtualenv did not produce a ybm command" \
                          "Expected $YBM_BIN. Try removing backend/.venv and running this again."

# Idempotent: creates config.yaml and tokens the first time, leaves them alone
# afterwards.
log "[2/3] Setting up config and tokens"
"$YBM_BIN" setup

log "[3/3] Starting YBM and opening the console"
START_ARGS=(start)
[ "$NO_BROWSER" = "0" ] && START_ARGS+=(--open)
"$YBM_BIN" "${START_ARGS[@]}" \
  || fail "startup failed" "Run '$YBM_BIN doctor' to diagnose. Logs: $HERE/.agent_control/logs"
