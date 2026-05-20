#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${GITEA_SERVER_TOKEN_FILE:-}" ]] && [[ -f "${GITEA_SERVER_TOKEN_FILE}" ]]; then
    export GITEA_SERVER_TOKEN="$(cat "${GITEA_SERVER_TOKEN_FILE}")"
    unset GITEA_SERVER_TOKEN_FILE
fi

exec litellm "$@"