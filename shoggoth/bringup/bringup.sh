#!/usr/bin/env bash
set -euo pipefail

generate_secret() {
    if [ ! -f "${1}" ]; then
        openssl rand -base64 32 | tr -dc 'a-zA-Z0-9' | head -c 32 > "${1}"
        chmod 600 "${1}"
    fi
}

generate_secret_hex() {
    if [ ! -f "${1}" ]; then
        openssl rand -hex 24 > "${1}"
        chmod 600 "${1}"
    fi
}

CLIENT_CONF=/tmp/client_conf

mkdir -p /shoggoth/bringup/auto_secrets
mkdir -p /shoggoth/bringup/rendered/secrets
mkdir -p /shoggoth/workflow

GITEA_SERVER_TOKEN="$(cat /run/secrets/gitea_server_token)"
export GITEA_SERVER_TOKEN
GITEA_USER="$(cat /shoggoth/private/gitea-user.txt)"
REDMINE_TOKEN="$(cat /shoggoth/private/redmine-token.txt)"

/shoggoth/setup-client.sh \
    --client-conf "${CLIENT_CONF}" \
    --host "${SHOGGOTH_DOMAIN}" --host-ip "${SHOGGOTH_IP}" \
    --gitea-token "${GITEA_SERVER_TOKEN}" --gitea-user "${GITEA_USER}" \
    --ai-token litellm \
    --redmine-token "${REDMINE_TOKEN}"

cp "${CLIENT_CONF}/env" /shoggoth/workflow/env
cp "${CLIENT_CONF}/apt-cache.conf" /shoggoth/workflow/apt-cache.conf
cp "${CLIENT_CONF}/qwen.json" /shoggoth/workflow/qwen-settings.json
cp -r /shoggoth/scripts /shoggoth/workflow/scripts
chown -R 1000:1000 /shoggoth/workflow

mkdir -p /shoggoth/bringup/rendered/unbound
envsubst '${SHOGGOTH_IP} ${SHOGGOTH_DOMAIN}' < /shoggoth/bringup/templates/unbound_local-data.conf > /shoggoth/bringup/rendered/unbound/local-data.conf
envsubst '${SHOGGOTH_DOMAIN}' < /shoggoth/bringup/templates/unbound.conf > /shoggoth/bringup/rendered/unbound/unbound.conf

generate_secret /shoggoth/bringup/auto_secrets/kestra-db-password.txt
generate_secret /shoggoth/bringup/auto_secrets/kestra-basic-auth-password.txt
mkdir -p /shoggoth/bringup/rendered/kestra
export KESTRA_DATASOURCES_POSTGRES_PASSWORD="$(cat /shoggoth/bringup/auto_secrets/kestra-db-password.txt)"
export KESTRA_BASIC_AUTH_PASSWORD="$(cat /shoggoth/bringup/auto_secrets/kestra-basic-auth-password.txt)"
envsubst '${KESTRA_DATASOURCES_POSTGRES_PASSWORD} ${KESTRA_BASIC_AUTH_PASSWORD} ${SHOGGOTH_DOMAIN}' \
    < /shoggoth/bringup/templates/kestra_config.yaml > /shoggoth/bringup/rendered/kestra/config.yaml
mkdir -p /shoggoth/bringup/rendered/secrets/kestra-db
rm -rf /shoggoth/bringup/rendered/secrets/kestra-db/password
cat /shoggoth/bringup/auto_secrets/kestra-db-password.txt > /shoggoth/bringup/rendered/secrets/kestra-db/password
chown 70:70 /shoggoth/bringup/rendered/secrets/kestra-db/password
chmod 400 /shoggoth/bringup/rendered/secrets/kestra-db/password

generate_secret /shoggoth/bringup/auto_secrets/redmine-db-password.txt
mkdir -p /shoggoth/bringup/rendered/secrets/redmine-db/redmine-db
rm -rf /shoggoth/bringup/rendered/secrets/redmine-db/redmine-db/password
cat /shoggoth/bringup/auto_secrets/redmine-db-password.txt > /shoggoth/bringup/rendered/secrets/redmine-db/redmine-db/password
chown 70:70 /shoggoth/bringup/rendered/secrets/redmine-db/redmine-db/password
chmod 400 /shoggoth/bringup/rendered/secrets/redmine-db/redmine-db/password
mkdir -p /shoggoth/bringup/rendered/secrets/redmine-db/redmine
rm -rf /shoggoth/bringup/rendered/secrets/redmine-db/redmine/password
cat /shoggoth/bringup/auto_secrets/redmine-db-password.txt > /shoggoth/bringup/rendered/secrets/redmine-db/redmine/password
chown 999:999 /shoggoth/bringup/rendered/secrets/redmine-db/redmine/password
chmod 400 /shoggoth/bringup/rendered/secrets/redmine-db/redmine/password

generate_secret /shoggoth/bringup/auto_secrets/grafana-admin-password.txt
mkdir -p /shoggoth/bringup/rendered/secrets/grafana
rm -rf /shoggoth/bringup/rendered/secrets/grafana/admin-password
cat /shoggoth/bringup/auto_secrets/grafana-admin-password.txt > /shoggoth/bringup/rendered/secrets/grafana/admin-password
chown 472:472 /shoggoth/bringup/rendered/secrets/grafana/admin-password
chmod 400 /shoggoth/bringup/rendered/secrets/grafana/admin-password

generate_secret_hex /shoggoth/bringup/auto_secrets/redmine-secret-key-base.txt
mkdir -p /shoggoth/bringup/rendered/secrets/redmine
rm -rf /shoggoth/bringup/rendered/secrets/redmine/secret-key-base
cat /shoggoth/bringup/auto_secrets/redmine-secret-key-base.txt > /shoggoth/bringup/rendered/secrets/redmine/secret-key-base
chown 999:999 /shoggoth/bringup/rendered/secrets/redmine/secret-key-base
chmod 400 /shoggoth/bringup/rendered/secrets/redmine/secret-key-base

generate_secret_hex /shoggoth/bringup/auto_secrets/gitea-runner-token.txt
mkdir -p /shoggoth/bringup/rendered/gitea-runner
envsubst '${SHOGGOTH_DOMAIN}' < /shoggoth/bringup/templates/gitea-runner_config.yaml > /shoggoth/bringup/rendered/gitea-runner/config.yaml
rm -rf /shoggoth/bringup/rendered/secrets/gitea-runner-token
cat /shoggoth/bringup/auto_secrets/gitea-runner-token.txt > /shoggoth/bringup/rendered/secrets/gitea-runner-token
chown 1000:1000 /shoggoth/bringup/rendered/secrets/gitea-runner-token

mkdir -p /shoggoth/bringup/rendered/litellm
envsubst '${GITEA_SERVER_TOKEN}' < /shoggoth/bringup/templates/litellm_config.yaml > /shoggoth/bringup/rendered/litellm/config.yaml

find /shoggoth/bringup/rendered -type d -exec chmod a+rx {} +
find /shoggoth/bringup/rendered -type f -not -path '*/secrets/*' -not -name 'config.yaml' -exec chmod a+r {} +
chmod 600 /shoggoth/bringup/rendered/litellm/config.yaml
chmod 600 /shoggoth/bringup/rendered/kestra/config.yaml
chmod 400 /shoggoth/bringup/rendered/secrets/kestra-db/password
chmod 400 /shoggoth/bringup/rendered/secrets/redmine-db/redmine-db/password
chmod 400 /shoggoth/bringup/rendered/secrets/redmine-db/redmine/password
chmod 400 /shoggoth/bringup/rendered/secrets/grafana/admin-password
chmod 400 /shoggoth/bringup/rendered/secrets/gitea-runner-token
chmod 400 /shoggoth/bringup/rendered/secrets/redmine/secret-key-base

mkdir -p /shoggoth/data/gitea
chown 1000:1000 /shoggoth/data/gitea

mkdir -p /shoggoth/data/gitea-runner
chown 1000:1000 /shoggoth/data/gitea-runner

mkdir -p /shoggoth/data/redmine-database
chown 70:70 /shoggoth/data/redmine-database

mkdir -p /shoggoth/data/redmine-files /shoggoth/data/redmine-plugins /shoggoth/data/redmine-bundle-cache
chown 999:999 /shoggoth/data/redmine-files /shoggoth/data/redmine-plugins /shoggoth/data/redmine-bundle-cache

mkdir -p /shoggoth/data/litellm
chown 1000:1000 /shoggoth/data/litellm

mkdir -p /shoggoth/data/ci-cache/ccws/pip
chown -R 1000:1000 /shoggoth/data/ci-cache