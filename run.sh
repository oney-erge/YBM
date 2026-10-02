#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./scripts/install-utils.sh
install_init "$PWD" "YBM"
install_enable_traps
action=run
case "${1:-}" in run|doctor|repair|docker|stop|logs) action=$1; shift ;; esac
no_browser=0
for arg in "$@"; do [ "$arg" = --no-browser ] && no_browser=1 || { echo "unknown option: $arg" >&2; exit 2; }; done
url=http://127.0.0.1:8765
health_check() { if command -v curl >/dev/null 2>&1; then curl -fsS --max-time 2 "$url/health" >/dev/null; else wget -qO- --timeout=2 "$url/health" >/dev/null; fi; }
wait_ready() { for _ in $(seq 1 180); do health_check 2>/dev/null && return; sleep 1; done; return 1; }
# open_url [target]: the console, signed in when a link carrying the token is given.
open_url() { local target="${1:-$url/admin}"; [ "$no_browser" -eq 1 ] && return; command -v open >/dev/null 2>&1 && open "$target" || command -v xdg-open >/dev/null 2>&1 && xdg-open "$target" || true; }
docker_running() { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 && [ -n "$(docker compose ps --quiet ybm 2>/dev/null)" ]; }
case "$action" in
  docker)
    command -v docker >/dev/null 2>&1 || { echo "Docker is not installed." >&2; exit 1; }
    docker info >/dev/null 2>&1 || { echo "Docker is installed but its engine is not running." >&2; exit 1; }
    install_lock
    install_require_space "$PWD" 3
    # No .env step: the container generates its own admin token and keeps it in
    # its state volume (scripts/docker-entrypoint.sh). Ask it for a signed-in link
    # rather than leaving the browser to prompt for a token.
    docker compose up --detach --build
    wait_ready || { docker compose logs ybm; echo "YBM did not become ready at $url." >&2; exit 1; }
    install_complete
    link="$(docker compose exec -T ybm ybm admin-url 2>/dev/null || true)"
    echo "YBM is ready at $url/admin"; open_url "${link:-$url/admin}"; exit 0 ;;
  stop) docker_running && exec docker compose down; [ -x backend/.venv/bin/ybm ] && exec backend/.venv/bin/ybm stop; echo "YBM is not installed or running."; exit 0 ;;
  logs) docker_running && exec docker compose logs --follow; [ -x backend/.venv/bin/ybm ] || { echo "YBM is not installed." >&2; exit 1; }; exec backend/.venv/bin/ybm logs backend --follow ;;
  doctor) [ -x backend/.venv/bin/ybm ] || { echo "YBM is not installed. Run ./run.sh once." >&2; exit 1; }; exec backend/.venv/bin/ybm doctor ;;
  repair) rm -f backend/.venv/.ybm_sync_fingerprint ;;
esac
# ${args[@]+"${args[@]}"}, not "${args[@]}": with `set -u`, macOS's stock bash
# 3.2 treats an empty array as unset and aborts with "unbound variable", which
# is the default (no --no-browser) case.
args=()
[ "$no_browser" -eq 1 ] && args+=(--no-browser)
exec ./ybm.sh ${args[@]+"${args[@]}"}
