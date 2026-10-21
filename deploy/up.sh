#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
command -v docker >/dev/null 2>&1 || { echo "Docker is required" >&2; exit 1; }
docker compose version >/dev/null
test -f .env.production || { echo "Missing .env.production" >&2; exit 1; }
test -f .admin-bootstrap.json || { echo "Missing .admin-bootstrap.json for administrator initialization" >&2; exit 1; }
chmod 600 .env.production .admin-bootstrap.json
compose() { docker compose --env-file .env.production -f compose.production.yml "$@"; }
compose config --quiet
compose build app
compose run --rm --no-deps app python scripts/production_check.py
compose up -d --wait app
compose exec -T app python scripts/bootstrap_admin.py < .admin-bootstrap.json
compose run --rm --no-deps proxy caddy validate --config /etc/caddy/Caddyfile
compose up -d --wait
compose ps
echo "Services started. Verify public HTTPS and log in with the private bootstrap credential file."
