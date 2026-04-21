#!/usr/bin/env bash
set -o pipefail
set -e

SSH_AGENT_PID=$(grep SSH_AGENT_PID ./private/env | sed 's/SSH_AGENT_PID=\([0-9]*\)/\1/' || true)

echo "SHOGGOTH_GIT_SSH_PORT=3022" > .env

echo "UID=`id -u`" >> .env
echo "GID=`id -g`" >> .env

echo "SHOGGOTH_ROOT=`pwd`" >> .env

echo "SHOGGOTH_DOMAIN=$1" >> .env
echo "SHOGGOTH_IP=$2" >> .env

# we dont know SSH_AGENT_PID in general
kill ${SSH_AGENT_PID} || true
eval $(ssh-agent -a $(mktemp -u -p $(pwd)/private/ ssh_auth_sock.XXX))

sed -i '/SSH_AUTH_SOCK/d' ./private/env
sed -i '/SSH_AGENT_PID/d' ./private/env
echo "SSH_AUTH_SOCK=${SSH_AUTH_SOCK}" >> .env
echo "SSH_AUTH_SOCK=${SSH_AUTH_SOCK}" >> ./private/env
echo "SSH_AGENT_PID=${SSH_AGENT_PID}" >> ./private/env

ssh-add ./private/ssh/id_rsa
