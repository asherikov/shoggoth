#!/usr/bin/env bash
set -euo pipefail

# substitute environment variables
sed \
    -e "s|\${SHOGGOTH_ROOT}|${SHOGGOTH_ROOT}|g" \
    -e "s|\${GITEA_SSH_AUTH_SOCK}|${GITEA_SSH_AUTH_SOCK}|g" \
    -e "s|\${GITEA_TOKEN_AUTH_SOCK}|${GITEA_TOKEN_AUTH_SOCK}|g" \
    /config.template.yaml > /config.yaml
export CONFIG_FILE=/config.yaml

# setup cache permissions assuming certain UID/GID
mkdir -p /cache/ccws/pip
chown -R 1000:1000 /cache/ccws

run.sh