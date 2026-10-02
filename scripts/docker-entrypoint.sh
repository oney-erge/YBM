#!/bin/sh
# Container entrypoint: make `docker compose up -d` work with nothing to
# configure.
#
# Previously the admin token and vault key had to be created by hand in a .env
# file before the container would start, and the console then asked for the token
# on first visit. Here both are generated once and kept in the state volume, so
# they survive recreating the container.
#
# The app reads /app/.env, which is a link to a file in that volume. That is what
# makes keys saved later through the console (an API key typed into the wizard)
# survive too: they used to be written to the container's own disk and lost the
# next time it was recreated. Anything already provided - compose's environment,
# or a .env you chose to make - wins and is left alone.
#
# The token is deliberately not printed: container logs are kept on disk. Print a
# signed-in link on demand with:  docker compose exec ybm ybm admin-url
set -eu

state="${YBM_STATE_DIR:-/app/.agent_control}"
app_env="${YBM_ENV_FILE:-/app/.env}"
state_env="$state/env"

mkdir -p "$state"
( umask 077; touch "$state_env" )
chmod 600 "$state_env"
ln -sfn "$state_env" "$app_env"

# ensure_secret NAME generator...: append NAME=<generated> unless it is already
# set in the environment or the env file.
ensure_secret() {
  name="$1"
  shift
  if [ -n "$(printenv "$name" || true)" ] || grep -q "^$name=" "$state_env"; then
    return 0
  fi
  printf '%s=%s\n' "$name" "$("$@")" >> "$state_env"
}

new_token() { python -c "import secrets; print(secrets.token_urlsafe(32))"; }
new_vault_key() { python -c "from agent_control.storage.secrets import SecretVault; print(SecretVault.generate_key())"; }

ensure_secret AGENT_ADMIN_TOKEN new_token
ensure_secret AGENT_SECRET_VAULT_KEY new_vault_key

# Create config.yaml from the shipped example when it is missing and pick a
# model from any provider API key that was passed in (llm/autodetect.py). Best
# effort: a bind-mounted ./config that this user cannot write to must not stop
# the container from starting - the first-run wizard reports that case itself.
ybm setup --quiet || echo "YBM: first-run setup could not finish; the console will show what is missing." >&2

echo "YBM is starting. Open the console, already signed in, with:"
echo "  docker compose exec ybm ybm admin-url"

exec "$@"
