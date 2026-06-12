#!/usr/bin/env bash
set -euo pipefail

export APP_KEY
export DB_PASSWORD
APP_KEY="$(cat /run/secrets/cdash_app_key)"
DB_PASSWORD="$(cat /run/secrets/cdash_db_password)"

exec /cdash/docker/docker-entrypoint.sh "$@"