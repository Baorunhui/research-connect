#!/usr/bin/env bash
# Prepare machine-local state for the standalone tool deployment.
# Idempotent: existing files under state/ are never overwritten.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created deploy/tool/.env from .env.example; fill in LLM_* and rerun." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1091
. ./.env
set +a

mkdir -p state/daily-paper state/empty

if [ ! -f state/htpasswd ]; then
  user="${TOOL_BASIC_AUTH_USER:-research}"
  pass="${TOOL_BASIC_AUTH_PASSWORD:-$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-16)}"
  printf '%s:%s\n' "$user" "$(openssl passwd -apr1 "$pass")" > state/htpasswd
  # Read by the unprivileged nginx worker inside the gateway container.
  chmod 644 state/htpasswd
  (umask 077 && printf 'user=%s\npassword=%s\n' "$user" "$pass" > state/credentials.txt)
  echo "Basic auth login written to deploy/tool/state/credentials.txt"
fi

config=state/daily-paper/config.yaml
if [ ! -f "$config" ]; then
  cp ../../modules/daily-paper-reader/docs_init/config.yaml "$config"
  if [ -n "${LLM_API_KEY:-}" ]; then
    # JSON strings are valid YAML scalars, so no YAML tooling is needed here.
    json() { python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"; }
    {
      echo "local:"
      echo "  chat:"
      echo "    base_url: $(json "${LLM_BASE_URL:-}")"
      echo "    api_key: $(json "${LLM_API_KEY}")"
      echo "    model: $(json "${LLM_MODEL:-}")"
    } >> "$config"
  fi
  chmod 600 "$config"
fi

echo "State ready. Start with: docker compose up -d --build"
