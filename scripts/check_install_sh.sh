#!/usr/bin/env bash
# Exercise the two helpers in scripts/install.sh that a syntax check cannot reach.
#
# Both broke the macOS/Linux one-liner without failing CI: a stray "+" replaced a line
# continuation in the curl command, and the CRLF line endings that a Windows runner
# writes into SHA256SUMS.txt stopped the file name from matching. No network is used.
#
# usage: scripts/check_install_sh.sh [path/to/install.sh]
set -euo pipefail

installer="${1:-scripts/install.sh}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

bash -n "$installer"

# Load just the helpers; the rest of install.sh downloads and installs for real.
sed -n '/^download_release() {/,/^}/p; /^expected_unix_sha256() {/,/^}/p' "$installer" > "$work/helpers.sh"
grep -q '^download_release()' "$work/helpers.sh" || { echo "FAIL: download_release not found" >&2; exit 1; }
grep -q '^expected_unix_sha256()' "$work/helpers.sh" || { echo "FAIL: expected_unix_sha256 not found" >&2; exit 1; }
# shellcheck disable=SC1091
. "$work/helpers.sh"

printf 'payload\n' > "$work/source.bin"
download_release "file://$work/source.bin" "$work/copy.bin" > /dev/null
cmp "$work/source.bin" "$work/copy.bin"
echo "ok: download_release copies a file"

printf 'AAAA1111  YBM-windows.zip\r\nBBBB2222  YBM-unix.tar.gz\r\nCCCC3333  YBM-Setup.msi\r\n' > "$work/crlf.txt"
printf 'AAAA1111  YBM-windows.zip\nBBBB2222  YBM-unix.tar.gz\n' > "$work/lf.txt"
printf 'DDDD4444  YBM-0.2.0-unix.tar.gz\n' > "$work/versioned.txt"
[ "$(expected_unix_sha256 "$work/crlf.txt")" = "bbbb2222" ] || { echo "FAIL: CRLF checksum file not parsed" >&2; exit 1; }
[ "$(expected_unix_sha256 "$work/lf.txt")" = "bbbb2222" ] || { echo "FAIL: LF checksum file not parsed" >&2; exit 1; }
[ "$(expected_unix_sha256 "$work/versioned.txt")" = "dddd4444" ] || { echo "FAIL: versioned archive name not parsed" >&2; exit 1; }
echo "ok: expected_unix_sha256 reads CRLF, LF, and versioned names"
