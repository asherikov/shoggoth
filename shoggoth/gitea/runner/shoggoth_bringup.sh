#!/bin/sh
set -euo pipefail

# substitute environment variables
if [ -n "${SHOGGOTH_ROOT:-}" ]; then
    sed "s|\${SHOGGOTH_ROOT}|${SHOGGOTH_ROOT}|g" /config.template.yaml > /config.yaml
    export CONFIG_FILE=/config.yaml
fi

# setup cache permissions assuming certain UID/GID
mkdir -p /cache/ccws/pip
chown -R 1000:1000 /cache/ccws

run.sh
