#!/bin/sh
set -eu

if [ -f /shoggoth_kestra_env ]; then
    set -a
    . /shoggoth_kestra_env
    set +a
fi

exec /app/kestra "$@"