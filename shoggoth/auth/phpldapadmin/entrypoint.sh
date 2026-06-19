#!/usr/bin/env bash
set -euo pipefail

set -a
. /shoggoth/bringup/rendered/phpldapadmin/phpldapadmin.env
set +a

exec /sbin/init-docker --config /etc/caddy/Caddyfile --adapter caddyfile