#!/bin/sh
set -euo pipefail

if [ -n "${SHOGGOTH_ROOT:-}" ]; then
    sed "s|\${SHOGGOTH_ROOT}|${SHOGGOTH_ROOT}|g" /config.template.yaml > /config.yaml
    export CONFIG_FILE=/config.yaml
fi

run.sh
