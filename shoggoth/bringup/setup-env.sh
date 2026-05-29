#!/usr/bin/env bash
set -o pipefail
set -e

echo "SHOGGOTH_GIT_SSH_PORT=3022" > .env

echo "SHOGGOTH_DOMAIN=$1" >> .env
echo "SHOGGOTH_IP=$2" >> .env