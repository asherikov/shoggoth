#!/usr/bin/env bash
set -euo pipefail

OPENBAO_ADDR="http://openbao:80"
OPENBAO_DATA="/shoggoth/openbao-data"
INIT_FLAG="${OPENBAO_DATA}/.shoggoth-initialized"
UNSEAL_KEY_FILE="${OPENBAO_DATA}/.shoggoth-unseal-key"
ROOT_TOKEN_FILE="${OPENBAO_DATA}/shoggoth/root-token"

bao_init() {
    if [ -f "${INIT_FLAG}" ]; then
        echo "OpenBao already initialized (flag file), unsealing..."
        bao_unseal
        return
    fi

    echo "Waiting for OpenBao API..."
    for i in $(seq 1 60); do
        if curl -sf "${OPENBAO_ADDR}/v1/sys/health" > /dev/null 2>&1; then
            echo "OpenBao is ready."
            break
        fi
        sleep 1
    done

    local health
    health="$(curl -s "${OPENBAO_ADDR}/v1/sys/health" 2>/dev/null || true)"
    if [ -z "${health}" ]; then
        echo "ERROR: OpenBao API never became available after 60s" >&2
        exit 1
    fi

    local initialized
    initialized="$(printf '%s' "${health}" | jq -r '.initialized // false')"
    if [ "${initialized}" = "true" ]; then
        if [ ! -f "${UNSEAL_KEY_FILE}" ]; then
            echo "ERROR: OpenBao is initialized but unseal key is missing. Wipe Raft data to reinitialize." >&2
            exit 1
        fi
        echo "OpenBao already initialized, unsealing..."
        bao_unseal
        touch "${INIT_FLAG}"
        return
    fi

    echo "Initializing OpenBao..."
    local init_output
    init_output="$(curl -sf -X PUT "${OPENBAO_ADDR}/v1/sys/init" \
        -H "Content-Type: application/json" \
        -d '{"secret_shares":1,"secret_threshold":1}')"

    local unseal_key root_token
    unseal_key="$(printf '%s' "${init_output}" | jq -r '.unseal_keys_b64[0]')"
    root_token="$(printf '%s' "${init_output}" | jq -r '.root_token')"

    if [ -z "${unseal_key}" ] || [ -z "${root_token}" ]; then
        echo "ERROR: Failed to parse init output" >&2
        echo "${init_output}" >&2
        exit 1
    fi

    printf '%s' "${unseal_key}" > "${UNSEAL_KEY_FILE}"
    chmod 400 "${UNSEAL_KEY_FILE}"

    printf '%s' "${root_token}" > "${ROOT_TOKEN_FILE}"
    chmod 400 "${ROOT_TOKEN_FILE}"

    echo "Unsealing OpenBao..."
    curl -sf -X PUT "${OPENBAO_ADDR}/v1/sys/unseal" \
        -H "Content-Type: application/json" \
        -d "{\"key\":\"${unseal_key}\"}" > /dev/null

    echo "Enabling KV v2 secrets engine at /secret..."
    curl -sf -X POST "${OPENBAO_ADDR}/v1/sys/mounts/secret" \
        -H "X-Vault-Token: ${root_token}" \
        -H "Content-Type: application/json" \
        -d '{"type":"kv-v2"}' > /dev/null 2>&1 || echo "KV v2 may already be enabled, continuing."

    touch "${INIT_FLAG}"
    echo "OpenBao initialized and unsealed successfully."
}

bao_unseal() {
    local unseal_key
    unseal_key="$(cat "${UNSEAL_KEY_FILE}")"
    curl -sf -X PUT "${OPENBAO_ADDR}/v1/sys/unseal" \
        -H "Content-Type: application/json" \
        -d "{\"key\":\"${unseal_key}\"}" > /dev/null 2>&1 || true
}

bao_get_value() {
    local path="${1}"
    local response
    response="$(curl -sf -H "X-Vault-Token: ${OPENBAO_TOKEN}" \
        "${OPENBAO_ADDR}/v1/secret/data/${path}")"
    printf '%s' "${response}" | jq -r '.data.data.value // empty'
}

bao_put() {
    local path="${1}"
    local key="${2}"
    local value="${3}"
    local payload
    payload="$(printf '%s' "${value}" | jq -Rs . | jq -c --arg k "${key}" '{"data": {($k): .}}')"
    curl -sf -H "X-Vault-Token: ${OPENBAO_TOKEN}" \
        -H "Content-Type: application/json" \
        -X POST \
        -d "${payload}" \
        "${OPENBAO_ADDR}/v1/secret/data/${path}" > /dev/null
}

bao_get_or_generate() {
    local path="${1}"
    local length="${2:-32}"
    local value
    value="$(bao_get_value "${path}" 2>/dev/null)" && [ -n "${value}" ] && {
        printf '%s' "${value}"
        return
    }
    value="$(openssl rand -base64 "${length}" | tr -dc 'a-zA-Z0-9' | head -c "${length}")"
    bao_put "${path}" "value" "${value}"
    printf '%s' "${value}"
}

bao_get_or_generate_hex() {
    local path="${1}"
    local length="${2:-24}"
    local value
    value="$(bao_get_value "${path}" 2>/dev/null)" && [ -n "${value}" ] && {
        printf '%s' "${value}"
        return
    }
    value="$(openssl rand -hex "${length}")"
    bao_put "${path}" "value" "${value}"
    printf '%s' "${value}"
}

render_secret() {
    local content="${1}"
    local path="${2}"
    local uid="${3}"
    local gid="${4}"
    mkdir -p "$(dirname "${path}")"
    printf '%s' "${content}" > "${path}"
    chown "${uid}:${gid}" "${path}"
    chmod 400 "${path}"
}

mkdir -p /shoggoth/bringup/rendered/secrets

bao_init

OPENBAO_TOKEN="$(cat "${ROOT_TOKEN_FILE}")"
export OPENBAO_TOKEN

GITEA_SERVER_TOKEN="$(cat /run/secrets/gitea_server_token)"
bao_put "gitea/server-token" "value" "${GITEA_SERVER_TOKEN}"
export GITEA_SERVER_TOKEN

REDMINE_TOKEN="$(cat /run/secrets/redmine_token)"
bao_put "redmine/api-token" "value" "${REDMINE_TOKEN}"
export REDMINE_TOKEN

mkdir -p /shoggoth/bringup/rendered/unbound
envsubst '${SHOGGOTH_DOMAIN}' < /shoggoth/bringup/templates/unbound.conf > /shoggoth/bringup/rendered/unbound/unbound.conf

KESTRA_DB_PASSWORD="$(bao_get_or_generate kestra/db-password)"
KESTRA_BASIC_AUTH_PASSWORD="$(bao_get_or_generate kestra/basic-auth-password)"
export KESTRA_DATASOURCES_POSTGRES_PASSWORD="${KESTRA_DB_PASSWORD}"
export KESTRA_BASIC_AUTH_PASSWORD
mkdir -p /shoggoth/bringup/rendered/kestra
envsubst '${KESTRA_DATASOURCES_POSTGRES_PASSWORD} ${KESTRA_BASIC_AUTH_PASSWORD} ${SHOGGOTH_DOMAIN}' \
    < /shoggoth/bringup/templates/kestra_config.yaml > /shoggoth/bringup/rendered/kestra/config.yaml
render_secret "${KESTRA_DB_PASSWORD}" /shoggoth/bringup/rendered/secrets/kestra-db/password 70 70

REDMINE_DB_PASSWORD="$(bao_get_or_generate redmine/db-password)"
render_secret "${REDMINE_DB_PASSWORD}" /shoggoth/bringup/rendered/secrets/redmine-db/redmine-db/password 70 70
render_secret "${REDMINE_DB_PASSWORD}" /shoggoth/bringup/rendered/secrets/redmine-db/redmine/password 999 999

GRAFANA_ADMIN_PASSWORD="$(bao_get_or_generate grafana/admin-password)"
render_secret "${GRAFANA_ADMIN_PASSWORD}" /shoggoth/bringup/rendered/secrets/grafana/admin-password 472 472

REDMINE_SECRET_KEY_BASE="$(bao_get_or_generate_hex redmine/secret-key-base)"
render_secret "${REDMINE_SECRET_KEY_BASE}" /shoggoth/bringup/rendered/secrets/redmine/secret-key-base 999 999

GITEA_RUNNER_TOKEN="$(bao_get_or_generate_hex gitea/runner-token 24)"
mkdir -p /shoggoth/bringup/rendered/gitea-runner
envsubst '${SHOGGOTH_DOMAIN}' < /shoggoth/bringup/templates/gitea-runner_config.yaml > /shoggoth/bringup/rendered/gitea-runner/config.yaml
render_secret "${GITEA_RUNNER_TOKEN}" /shoggoth/bringup/rendered/secrets/gitea-runner-token 1000 1000

mkdir -p /shoggoth/bringup/rendered/litellm
envsubst '${GITEA_SERVER_TOKEN}' < /shoggoth/bringup/templates/litellm_config.yaml > /shoggoth/bringup/rendered/litellm/config.yaml

mkdir -p /shoggoth/bringup/rendered/angie
envsubst '${SHOGGOTH_DOMAIN} ${GITEA_SERVER_TOKEN} ${REDMINE_TOKEN}' < /shoggoth/bringup/templates/angie.conf > /shoggoth/bringup/rendered/angie/angie.conf

CDASH_DB_PASSWORD="$(bao_get_or_generate cdash/db-password)"
CDASH_APP_KEY="$(bao_get_or_generate cdash/app-key)"
render_secret "${CDASH_DB_PASSWORD}" /shoggoth/bringup/rendered/secrets/cdash-db/password 70 70
render_secret "base64:$(printf '%s' "${CDASH_APP_KEY}" | base64 -w0)" /shoggoth/bringup/rendered/secrets/cdash/app-key 33 33
render_secret "${CDASH_DB_PASSWORD}" /shoggoth/bringup/rendered/secrets/cdash/db-password 33 33

mkdir -p /shoggoth/vpn/wireguard/data

find /shoggoth/bringup/rendered -type d -exec chmod a+rx {} +
find /shoggoth/bringup/rendered -type f -not -path '*/secrets/*' -not -name 'config.yaml' -not -name '*.sh' -exec chmod a+r {} +
chmod 600 /shoggoth/bringup/rendered/litellm/config.yaml
chmod 600 /shoggoth/bringup/rendered/kestra/config.yaml
chmod 600 /shoggoth/bringup/rendered/angie/angie.conf

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