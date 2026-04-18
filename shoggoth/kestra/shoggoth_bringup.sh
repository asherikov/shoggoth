#!/usr/bin/env bash
set -euo pipefail

export KESTRA_DATASOURCES_POSTGRES_PASSWORD="$(cat /run/secrets/kestra_db_password)"
export KESTRA_BASIC_AUTH_PASSWORD="$(cat /run/secrets/kestra_basic_auth_password)"

/app/kestra plugins install io.kestra.plugin:plugin-docker:LATEST
exec /app/kestra server standalone --config /shoggoth_kestra.yaml --flow-path /app/flows "$@"


