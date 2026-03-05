#!/bin/sh

set -e

cat > /opt/unbound/etc/unbound/local-data.conf <<EOF
local-data: "shoggoth.local. 86400 IN A ${SHOGGOTH_IP}"
EOF

/unbound.sh
