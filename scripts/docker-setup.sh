#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="$ROOT_DIR/docker/config"
CONFIG_FILE="$CONFIG_DIR/connect-hub.env"
DAILY_CONFIG_FILE="$CONFIG_DIR/daily-paper.yaml"
COMPOSE_ENV="$ROOT_DIR/.env"

mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_FILE" ]; then
  cp "$ROOT_DIR/apps/connect-hub/.env.example" "$CONFIG_FILE"
  chmod 600 "$CONFIG_FILE" 2>/dev/null || true
  printf 'Created Docker configuration: %s\n' "$CONFIG_FILE"
else
  printf 'Keeping existing Docker configuration: %s\n' "$CONFIG_FILE"
fi
if [ ! -f "$DAILY_CONFIG_FILE" ]; then
  cp "$ROOT_DIR/modules/daily-paper-reader/docs_init/config.yaml" "$DAILY_CONFIG_FILE"
  chmod 600 "$DAILY_CONFIG_FILE" 2>/dev/null || true
  printf 'Created Daily Paper configuration: %s\n' "$DAILY_CONFIG_FILE"
fi

TEMP_ENV="$(mktemp "$ROOT_DIR/.env.docker.XXXXXX")"
if [ -f "$COMPOSE_ENV" ]; then
  grep -Ev '^(RESEARCH_CONNECT_UID|RESEARCH_CONNECT_GID)=' "$COMPOSE_ENV" > "$TEMP_ENV" || true
fi
printf 'RESEARCH_CONNECT_UID=%s\n' "$(id -u)" >> "$TEMP_ENV"
printf 'RESEARCH_CONNECT_GID=%s\n' "$(id -g)" >> "$TEMP_ENV"
mv "$TEMP_ENV" "$COMPOSE_ENV"
chmod 600 "$COMPOSE_ENV" 2>/dev/null || true

printf 'Docker host UID/GID saved in %s\n' "$COMPOSE_ENV"
printf 'Next: edit %s, then run docker compose build\n' "$CONFIG_FILE"
