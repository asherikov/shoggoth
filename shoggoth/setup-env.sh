#!/usr/bin/env bash
set -o pipefail
set -e

echo "SHOGGOTH_GIT_SSH_PORT=3022" > .env

echo "UID=`id -u`" >> .env
echo "GID=`id -g`" >> .env

echo "SHOGGOTH_ROOT=`pwd`" >> .env

echo "SHOGGOTH_DOMAIN=$1" >> .env
echo "SHOGGOTH_IP=$2" >> .env

# we dont know SSH_AGENT_PID in general
if [ -f ./private/ssh_agent_pid ]
then
    kill $(cat ./private/ssh_agent_pid) || true
fi
rm -rf private/ssh_auth_sock* private/git_credential_sock*

eval $(ssh-agent -a $(pwd)/private/ssh_auth_sock)

GITEA_TOKEN_AUTH_SOCK="$(pwd)/private/git_credential_sock"

chmod 700 ./private
# https://unix.stackexchange.com/questions/269805/how-can-i-detach-a-process-from-a-bash-script
(set -m && git credential-cache--daemon "${GITEA_TOKEN_AUTH_SOCK}" &)

GITEA_SERVER_TOKEN=$(grep GITEA_SERVER_TOKEN ./private/env | sed 's/GITEA_SERVER_TOKEN=\([0-9]*\)/\1/')
printf "protocol=http\nhost=git.%s\nusername=token\npassword=%s\n" "$1" "$GITEA_SERVER_TOKEN" \
    | git credential-cache --socket "${GITEA_TOKEN_AUTH_SOCK}" --timeout 31536000 store
ls -l "${GITEA_TOKEN_AUTH_SOCK}"

echo "GITEA_SSH_AUTH_SOCK=${SSH_AUTH_SOCK}" >> .env
echo "GITEA_TOKEN_AUTH_SOCK=${GITEA_TOKEN_AUTH_SOCK}" >> .env

sed -i '/SSH_AUTH_SOCK/d' ./private/env
echo "SSH_AUTH_SOCK=${SSH_AUTH_SOCK}" >> ./private/env

echo "${SSH_AGENT_PID}" > ./private/ssh_agent_pid

ssh-add ./private/ssh/id_rsa

