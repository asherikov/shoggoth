#!/usr/bin/env bash
set -euo pipefail

OPENBAO_ADDR="http://openbao:80"
OPENBAO_DATA="/shoggoth/openbao-data"
INIT_FLAG="${OPENBAO_DATA}/.shoggoth-initialized"
UNSEAL_KEY_FILE="${OPENBAO_DATA}/.shoggoth-unseal-key"
ROOT_TOKEN_FILE="${OPENBAO_DATA}/shoggoth/root-token"

. /shoggoth/bringup/bao_helpers.sh

bao_wait_for_api() {
    echo "shoggoth: Waiting for OpenBao API..."
    for i in $(seq 1 60); do
        local http_code
        http_code="$(curl -s -o /dev/null -w '%{http_code}' "${OPENBAO_ADDR}/v1/sys/health" 2>/dev/null || true)"
        if [ -n "${http_code}" ] && [ "${http_code}" != "000" ]; then
            echo "shoggoth: OpenBao is ready (HTTP ${http_code})."
            return 0
        fi
        sleep 1
    done
    echo "shoggoth: ERROR: OpenBao API never became available after 60s" >&2
    return 1
}

bao_init() {
    if [ -f "${INIT_FLAG}" ]; then
        echo "shoggoth: OpenBao already initialized (flag file), unsealing..."
        bao_wait_for_api || exit 1
        bao_unseal
        return
    fi

    bao_wait_for_api || exit 1

    local health
    health="$(curl -s "${OPENBAO_ADDR}/v1/sys/health" 2>/dev/null || true)"
    if [ -z "${health}" ]; then
        echo "shoggoth: ERROR: OpenBao API never became available after 60s" >&2
        exit 1
    fi

    local initialized
    initialized="$(printf '%s' "${health}" | jq -r '.initialized // false')"
    if [ "${initialized}" = "true" ]; then
        if [ ! -f "${UNSEAL_KEY_FILE}" ]; then
            echo "shoggoth: ERROR: OpenBao is initialized but unseal key is missing. Wipe Raft data to reinitialize." >&2
            exit 1
        fi
        echo "shoggoth: OpenBao already initialized, unsealing..."
        bao_unseal
        touch "${INIT_FLAG}"
        return
    fi

    echo "shoggoth: Initializing OpenBao..."
    local init_output
    init_output="$(curl -sf -X PUT "${OPENBAO_ADDR}/v1/sys/init" \
        -H "Content-Type: application/json" \
        -d '{"secret_shares":1,"secret_threshold":1}')"

    local unseal_key root_token
    unseal_key="$(printf '%s' "${init_output}" | jq -r '.unseal_keys_b64[0]')"
    root_token="$(printf '%s' "${init_output}" | jq -r '.root_token')"

    if [ -z "${unseal_key}" ] || [ -z "${root_token}" ]; then
        echo "shoggoth: ERROR: Failed to parse init output" >&2
        echo "shoggoth: ${init_output}" >&2
        exit 1
    fi

    printf '%s' "${unseal_key}" > "${UNSEAL_KEY_FILE}"
    chmod 400 "${UNSEAL_KEY_FILE}"

    printf '%s' "${root_token}" > "${ROOT_TOKEN_FILE}"
    chmod 400 "${ROOT_TOKEN_FILE}"

    echo "shoggoth: Unsealing OpenBao..."
    curl -sf -X PUT "${OPENBAO_ADDR}/v1/sys/unseal" \
        -H "Content-Type: application/json" \
        -d "{\"key\":\"${unseal_key}\"}" > /dev/null

    echo "shoggoth: Enabling KV v2 secrets engine at /secret..."
    curl -sf -X POST "${OPENBAO_ADDR}/v1/sys/mounts/secret" \
        -H "X-Vault-Token: ${root_token}" \
        -H "Content-Type: application/json" \
        -d '{"type":"kv-v2"}' > /dev/null 2>&1 || echo "shoggoth: KV v2 may already be enabled, continuing."

    touch "${INIT_FLAG}"
    echo "shoggoth: OpenBao initialized and unsealed successfully."
}

bao_unseal() {
    local unseal_key
    unseal_key="$(cat "${UNSEAL_KEY_FILE}")"
    local unseal_response
    unseal_response="$(curl -sf -X PUT "${OPENBAO_ADDR}/v1/sys/unseal" \
        -H "Content-Type: application/json" \
        -d "{\"key\":\"${unseal_key}\"}")" || {
        echo "shoggoth: ERROR: OpenBao unseal request failed (API not ready at ${OPENBAO_ADDR})" >&2
        exit 1
    }
    if ! printf '%s' "${unseal_response}" | jq -e '.sealed == false' > /dev/null 2>&1; then
        echo "shoggoth: ERROR: OpenBao unseal failed (still sealed after submitting key)" >&2
        exit 1
    fi
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

SHOGGOTH_VAULT_TOKEN="$(cat "${ROOT_TOKEN_FILE}")"
export SHOGGOTH_VAULT_TOKEN

GITEA_SERVER_TOKEN="$(cat /run/secrets/gitea_server_token)"
bao_put "gitea/server-token" "value" "${GITEA_SERVER_TOKEN}"
export GITEA_SERVER_TOKEN

REDMINE_TOKEN="$(cat /run/secrets/redmine_token)"
bao_put "redmine/api-token" "value" "${REDMINE_TOKEN}"
export REDMINE_TOKEN

OLLAMA_CLOUD_TOKEN="$(cat /run/secrets/ollama_cloud_token)"
bao_put "ollama/cloud-token" "value" "${OLLAMA_CLOUD_TOKEN}"
export OLLAMA_CLOUD_TOKEN

mkdir -p /shoggoth/bringup/rendered/unbound
envsubst '${SHOGGOTH_DOMAIN}' < /shoggoth/bringup/templates/unbound.conf > /shoggoth/bringup/rendered/unbound/unbound.conf

KESTRA_DB_PASSWORD="$(bao_get_or_generate kestra/db-password)"
export KESTRA_DATASOURCES_POSTGRES_PASSWORD="${KESTRA_DB_PASSWORD}"
render_secret "${KESTRA_DB_PASSWORD}" /shoggoth/bringup/rendered/secrets/kestra-db/password 70 70

REDMINE_DB_PASSWORD="$(bao_get_or_generate redmine/db-password)"
render_secret "${REDMINE_DB_PASSWORD}" /shoggoth/bringup/rendered/secrets/redmine-db/redmine-db/password 70 70
render_secret "${REDMINE_DB_PASSWORD}" /shoggoth/bringup/rendered/secrets/redmine-db/redmine/password 999 999

REDMINE_SECRET_KEY_BASE="$(bao_get_or_generate_hex redmine/secret-key-base)"
render_secret "${REDMINE_SECRET_KEY_BASE}" /shoggoth/bringup/rendered/secrets/redmine/secret-key-base 999 999

GITEA_RUNNER_TOKEN="$(bao_get_or_generate_hex gitea/runner-token 24)"
mkdir -p /shoggoth/bringup/rendered/gitea-runner
envsubst '${SHOGGOTH_DOMAIN}' < /shoggoth/bringup/templates/gitea-runner_config.yaml > /shoggoth/bringup/rendered/gitea-runner/config.yaml
render_secret "${GITEA_RUNNER_TOKEN}" /shoggoth/bringup/rendered/secrets/gitea-runner-token 1000 1000

mkdir -p /shoggoth/bringup/rendered/litellm
envsubst '${GITEA_SERVER_TOKEN} ${OLLAMA_CLOUD_TOKEN}' < /shoggoth/bringup/templates/litellm_config.yaml > /shoggoth/bringup/rendered/litellm/config.yaml

mkdir -p /shoggoth/bringup/rendered/web-internal
envsubst '${SHOGGOTH_DOMAIN} ${GITEA_SERVER_TOKEN} ${REDMINE_TOKEN}' < /shoggoth/bringup/templates/web-internal.conf > /shoggoth/bringup/rendered/web-internal/web-internal.conf

echo "shoggoth: Generating shared CA and TLS certificate..."
CERT_DIR="/shoggoth/bringup/rendered/certs"
DOCKER_CACHE_CA_DIR="/shoggoth/docker/cache/certs"
mkdir -p "${CERT_DIR}" "${DOCKER_CACHE_CA_DIR}"
CA_PASS="$(bao_get_or_generate ca/passphrase)"
if [ ! -f "${CERT_DIR}/shoggoth-ca.key" ] || [ ! -f "${CERT_DIR}/shoggoth-ca.crt" ]; then
    openssl genrsa -des3 -passout "pass:${CA_PASS}" -out "${CERT_DIR}/shoggoth-ca.key" 4096 || { echo "shoggoth: FATAL: CA key generation failed" >&2; exit 1; }
    openssl req -new -x509 -days 3650 -sha256 \
        -key "${CERT_DIR}/shoggoth-ca.key" -passin "pass:${CA_PASS}" \
        -out "${CERT_DIR}/shoggoth-ca.crt" \
        -subj "/CN=shoggoth CA" \
        -extensions v3_ca \
        -config <(printf '[req]\ndistinguished_name=dn\n[dn]\n[v3_ca]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,digitalSignature,cRLSign,keyCertSign\nsubjectKeyIdentifier=hash\n') || { echo "shoggoth: FATAL: CA certificate generation failed" >&2; exit 1; }
fi
if [ ! -f "${CERT_DIR}/shoggoth.key" ] || [ ! -f "${CERT_DIR}/shoggoth.crt" ]; then
    openssl genrsa -out "${CERT_DIR}/shoggoth.key" 2048 || { echo "shoggoth: FATAL: server key generation failed" >&2; exit 1; }
    openssl req -new -key "${CERT_DIR}/shoggoth.key" \
        -out "${CERT_DIR}/shoggoth.csr" \
        -subj "/CN=*.${SHOGGOTH_DOMAIN}" || { echo "shoggoth: FATAL: CSR generation failed" >&2; exit 1; }
    SAN_ENTRIES="DNS:*.${SHOGGOTH_DOMAIN},DNS:${SHOGGOTH_DOMAIN}"
    openssl x509 -req -days 3650 -sha256 \
        -in "${CERT_DIR}/shoggoth.csr" \
        -CA "${CERT_DIR}/shoggoth-ca.crt" \
        -CAkey "${CERT_DIR}/shoggoth-ca.key" \
        -passin "pass:${CA_PASS}" \
        -out "${CERT_DIR}/shoggoth.crt" \
        -extfile <(printf "subjectAltName=${SAN_ENTRIES}\n") || { echo "shoggoth: FATAL: server certificate generation failed" >&2; exit 1; }
    rm -f "${CERT_DIR}/shoggoth.csr"
fi
if [ ! -f "${DOCKER_CACHE_CA_DIR}/ca.key" ] || [ ! -f "${DOCKER_CACHE_CA_DIR}/ca.crt" ]; then
    openssl rsa -in "${CERT_DIR}/shoggoth-ca.key" -passin "pass:${CA_PASS}" -des3 -passout "pass:foobar" -out "${DOCKER_CACHE_CA_DIR}/ca.key" || { echo "shoggoth: FATAL: docker-cache CA key re-encryption failed" >&2; exit 1; }
    cp "${CERT_DIR}/shoggoth-ca.crt" "${DOCKER_CACHE_CA_DIR}/ca.crt"
    echo "01" > "${DOCKER_CACHE_CA_DIR}/ca.srl"
    chmod 600 "${DOCKER_CACHE_CA_DIR}/ca.key"
    chmod 644 "${DOCKER_CACHE_CA_DIR}/ca.crt" "${DOCKER_CACHE_CA_DIR}/ca.srl"
fi
chmod 600 "${CERT_DIR}/shoggoth-ca.key" "${CERT_DIR}/shoggoth.key"
chmod 644 "${CERT_DIR}/shoggoth-ca.crt" "${CERT_DIR}/shoggoth.crt"
mkdir -p /shoggoth/bringup/rendered/web-external
cp /shoggoth/bringup/templates/web-external.conf /shoggoth/bringup/rendered/web-external/web-external.conf
chmod 644 /shoggoth/bringup/rendered/web-external/web-external.conf

CDASH_DB_PASSWORD="$(bao_get_or_generate cdash/db-password)"
CDASH_APP_KEY="$(bao_get_or_generate cdash/app-key)"
CDASH_APP_KEY_RENDERED="base64:$(printf '%s' "${CDASH_APP_KEY}" | base64 -w0)"
render_secret "${CDASH_DB_PASSWORD}" /shoggoth/bringup/rendered/secrets/cdash-db/password 70 70
render_secret "${CDASH_APP_KEY_RENDERED}" /shoggoth/bringup/rendered/secrets/cdash/app-key 33 33
render_secret "${CDASH_DB_PASSWORD}" /shoggoth/bringup/rendered/secrets/cdash/db-password 33 33

SHOGGOTH_ADMIN_PASSWORD="$(bao_get_or_generate openldap/admin-password)"
if [ -f /shoggoth/private/admin-password.txt ]; then
    SHOGGOTH_ADMIN_PASSWORD="$(tr -d '\n\r' < /shoggoth/private/admin-password.txt)"
    bao_put "openldap/admin-password" "value" "${SHOGGOTH_ADMIN_PASSWORD}"
fi
OPENLDAP_S_LDAPAUTH_PASSWORD="$(bao_get_or_generate openldap/sldapauth-password)"
OPENLDAP_S_SLAVE_PASSWORD="$(bao_get_or_generate openldap/sslave-password)"
render_secret "${SHOGGOTH_ADMIN_PASSWORD}" /shoggoth/bringup/rendered/secrets/openldap/admin-password 911 911
OPENLDAP_CONFIG_ADMIN_PASSWORD="$(bao_get_or_generate openldap/config-admin-password)"
render_secret "${OPENLDAP_CONFIG_ADMIN_PASSWORD}" /shoggoth/bringup/rendered/secrets/openldap-config-admin-password 911 911
render_secret "${SHOGGOTH_ADMIN_PASSWORD}" /shoggoth/bringup/rendered/secrets/grafana/admin-password 472 472
render_secret "${SHOGGOTH_ADMIN_PASSWORD}" /shoggoth/bringup/rendered/secrets/wireguard/admin-password 0 0
export SHOGGOTH_ADMIN_PASSWORD
mkdir -p /shoggoth/bringup/rendered/kestra
envsubst '${KESTRA_DATASOURCES_POSTGRES_PASSWORD} ${SHOGGOTH_ADMIN_PASSWORD} ${SHOGGOTH_DOMAIN}' \
    < /shoggoth/bringup/templates/kestra_config.yaml > /shoggoth/bringup/rendered/kestra/config.yaml
export LDAP_BASE_DN="$(printf '%s' "${SHOGGOTH_DOMAIN}" | sed 's/\./,dc=/g; s/^/dc=/')"
export LDAP_BIND_DN="uid=sldapauth,ou=people,${LDAP_BASE_DN}"
export LDAP_HOST="openldap.${SHOGGOTH_DOMAIN}"

render_ldap_env() {
    local path="${1}"
    shift
    mkdir -p "$(dirname "${path}")"
    {
        printf 'LDAP_BASE_DN=%s\n' "${LDAP_BASE_DN}"
        printf 'LDAP_HOST=%s\n' "${LDAP_HOST}"
        printf 'LDAP_BIND_DN=%s\n' "${LDAP_BIND_DN}"
        printf 'LDAP_BIND_PASSWORD=%s\n' "${OPENLDAP_S_LDAPAUTH_PASSWORD}"
    } > "${path}"
    printf '%s' "${path}"
}

render_ldap_env /shoggoth/bringup/rendered/cdash/cdash.env
{
    printf 'APP_KEY=%s\n' "${CDASH_APP_KEY_RENDERED}"
    printf 'DB_PASSWORD=%s\n' "${CDASH_DB_PASSWORD}"
    printf 'CDASH_AUTHENTICATION_PROVIDER=ldap\n'
    printf 'LOGIN_FIELD=Username\n'
    printf 'LDAP_HOSTS=%s\n' "${LDAP_HOST}"
    printf 'LDAP_PORT=389\n'
    printf 'LDAP_PROVIDER=openldap\n'
    printf 'LDAP_LOCATE_USERS_BY=uid\n'
    printf 'LDAP_USERNAME=%s\n' "${LDAP_BIND_DN}"
    printf 'LDAP_PASSWORD=%s\n' "${OPENLDAP_S_LDAPAUTH_PASSWORD}"
} >> /shoggoth/bringup/rendered/cdash/cdash.env

render_ldap_env /shoggoth/bringup/rendered/redmine/redmine.env
printf 'SHOGGOTH_ADMIN_PASSWORD=%s\n' "${SHOGGOTH_ADMIN_PASSWORD}" >> /shoggoth/bringup/rendered/redmine/redmine.env

render_ldap_env /shoggoth/bringup/rendered/gitea/gitea.env
printf 'SHOGGOTH_ADMIN_PASSWORD=%s\n' "${SHOGGOTH_ADMIN_PASSWORD}" >> /shoggoth/bringup/rendered/gitea/gitea.env

mkdir -p /shoggoth/bringup/rendered/phpldapadmin
{
    printf 'LDAP_HOST=%s\n' "${LDAP_HOST}"
    printf 'LDAP_USERNAME=%s\n' "${LDAP_BIND_DN}"
    printf 'LDAP_PASSWORD=%s\n' "${OPENLDAP_S_LDAPAUTH_PASSWORD}"
} > /shoggoth/bringup/rendered/phpldapadmin/phpldapadmin.env
chmod 444 /shoggoth/bringup/rendered/phpldapadmin/phpldapadmin.env

echo "shoggoth: Generating OpenLDAP LDIF files..."

ADMIN_PASS_HASH="$(slappasswd -s "${SHOGGOTH_ADMIN_PASSWORD}")"
SLDAPAUTH_PASS_HASH="$(slappasswd -s "${OPENLDAP_S_LDAPAUTH_PASSWORD}")"
SSLAVE_PASS_HASH="$(slappasswd -s "${OPENLDAP_S_SLAVE_PASSWORD}")"
export ADMIN_PASS_HASH SLDAPAUTH_PASS_HASH SSLAVE_PASS_HASH

MDB_DN="olcDatabase={1}mdb,cn=config"
export MDB_DN

mkdir -p /shoggoth/bringup/rendered/openldap

envsubst '${LDAP_BASE_DN}' \
    < /shoggoth/bringup/templates/openldap_config.ldif \
    > /shoggoth/bringup/rendered/openldap/config.ldif

envsubst '${LDAP_BASE_DN} ${SHOGGOTH_DOMAIN}' \
    < /shoggoth/bringup/templates/openldap_data.ldif \
    > /shoggoth/bringup/rendered/openldap/data.ldif

envsubst '${LDAP_BASE_DN} ${ADMIN_PASS_HASH} ${SLDAPAUTH_PASS_HASH} ${SSLAVE_PASS_HASH}' \
    < /shoggoth/bringup/templates/openldap_passwords.ldif \
    > /shoggoth/bringup/rendered/openldap/passwords.ldif

envsubst '${MDB_DN}' \
    < /shoggoth/bringup/templates/openldap_memberof_module.ldif \
    > /shoggoth/bringup/rendered/openldap/memberof_module.ldif

envsubst '${MDB_DN}' \
    < /shoggoth/bringup/templates/openldap_memberof_overlay.ldif \
    > /shoggoth/bringup/rendered/openldap/memberof_overlay.ldif

chmod 400 /shoggoth/bringup/rendered/openldap/config.ldif
chmod 400 /shoggoth/bringup/rendered/openldap/data.ldif
chmod 400 /shoggoth/bringup/rendered/openldap/passwords.ldif
chmod 400 /shoggoth/bringup/rendered/openldap/memberof_module.ldif
chmod 400 /shoggoth/bringup/rendered/openldap/memberof_overlay.ldif

echo "shoggoth: Configuring OpenBao LDAP auth method..."

LDAP_AUTH_ENABLED="$(curl -sfS -H "X-Vault-Token: ${SHOGGOTH_VAULT_TOKEN}" \
    "${OPENBAO_ADDR}/v1/sys/auth" \
    | jq -r 'has("ldap/")')"

if [ "${LDAP_AUTH_ENABLED}" != "true" ]; then
    curl -sfS -X POST "${OPENBAO_ADDR}/v1/sys/auth/ldap" \
        -H "X-Vault-Token: ${SHOGGOTH_VAULT_TOKEN}" \
        -H "Content-Type: application/json" \
        -d '{"type":"ldap"}' > /dev/null
fi

echo "shoggoth: Creating shoggoth-admin policy in OpenBao..."
POLICY_PAYLOAD="$(jq -n \
    --arg policy $'path "secret/data/*" { capabilities = ["create", "read", "update", "delete", "list"] }\npath "sys/*" { capabilities = ["read", "list"] }' \
    '{policy: $policy}')"
POLICY_FILE="$(mktemp)"
printf '%s' "${POLICY_PAYLOAD}" > "${POLICY_FILE}"
chmod 400 "${POLICY_FILE}"
curl -sfS -X PUT "${OPENBAO_ADDR}/v1/sys/policies/acl/shoggoth-admin" \
    -H "X-Vault-Token: ${SHOGGOTH_VAULT_TOKEN}" \
    -H "Content-Type: application/json" \
    -d @"${POLICY_FILE}" > /dev/null
rm -f "${POLICY_FILE}"

echo "shoggoth: Configuring OpenBao LDAP auth connection..."
LDAP_CONFIG_PAYLOAD="$(jq -n \
    --arg url "ldap://${LDAP_HOST}:389" \
    --arg binddn "${LDAP_BIND_DN}" \
    --arg bindpass "${OPENLDAP_S_LDAPAUTH_PASSWORD}" \
    --arg userdn "ou=people,${LDAP_BASE_DN}" \
    --arg groupdn "ou=groups,${LDAP_BASE_DN}" \
    '{url: $url, binddn: $binddn, bindpass: $bindpass, userdn: $userdn, userattr: "uid", groupdn: $groupdn, groupattr: "cn", groupfilter: "(member={{.UserDN}})", insecure_tls: false, starttls: false, deny_null_bind: true, username_as_alias: true}')"
LDAP_CONFIG_FILE="$(mktemp)"
printf '%s' "${LDAP_CONFIG_PAYLOAD}" > "${LDAP_CONFIG_FILE}"
chmod 400 "${LDAP_CONFIG_FILE}"
curl -sfS -X POST "${OPENBAO_ADDR}/v1/auth/ldap/config" \
    -H "X-Vault-Token: ${SHOGGOTH_VAULT_TOKEN}" \
    -H "Content-Type: application/json" \
    -d @"${LDAP_CONFIG_FILE}" > /dev/null
rm -f "${LDAP_CONFIG_FILE}"

echo "shoggoth: Mapping admins group to shoggoth-admin policy in OpenBao..."
curl -sfS -X POST "${OPENBAO_ADDR}/v1/auth/ldap/groups/admins" \
    -H "X-Vault-Token: ${SHOGGOTH_VAULT_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{"policies":"shoggoth-admin"}' > /dev/null

echo "shoggoth: Verifying OpenBao LDAP auth configuration..."
LDAP_CONFIG_URL="$(curl -sfS -H "X-Vault-Token: ${SHOGGOTH_VAULT_TOKEN}" \
    "${OPENBAO_ADDR}/v1/auth/ldap/config" | jq -r '.data.url // "MISSING"')"
echo "shoggoth: OpenBao LDAP auth URL: ${LDAP_CONFIG_URL}"

mkdir -p /shoggoth/vpn/wireguard/data

find /shoggoth/bringup/rendered -type d -exec chmod a+rx {} +
find /shoggoth/bringup/rendered -type f -not -path '*/secrets/*' -not -path '*/certs/*' -not -name 'config.yaml' -not -name '*.sh' -not -name 'cdash.env' -not -name 'redmine.env' -not -name 'gitea.env' -not -name 'phpldapadmin.env' -not -path '*/openldap/*.ldif' -exec chmod a+r {} +
chown 1000:1000 /shoggoth/bringup/rendered/gitea/gitea.env
chmod 400 /shoggoth/bringup/rendered/gitea/gitea.env
chown 999:999 /shoggoth/bringup/rendered/redmine/redmine.env
chmod 400 /shoggoth/bringup/rendered/redmine/redmine.env
chown 33:33 /shoggoth/bringup/rendered/cdash/cdash.env
chmod 400 /shoggoth/bringup/rendered/cdash/cdash.env
chmod 600 /shoggoth/bringup/rendered/litellm/config.yaml
chmod 600 /shoggoth/bringup/rendered/kestra/config.yaml
chmod 600 /shoggoth/bringup/rendered/web-internal/web-internal.conf

echo "shoggoth: Creating Qwen Code plugin archive..."
mkdir -p /shoggoth/bringup/rendered/plugin
PLUGIN_STAGE="$(mktemp -d)"
cp -a /shoggoth/ai/plugin/. "${PLUGIN_STAGE}/"
envsubst '${SHOGGOTH_DOMAIN}' < /shoggoth/bringup/templates/qwen-extension.json > "${PLUGIN_STAGE}/qwen-extension.json"
rm -f /shoggoth/bringup/rendered/plugin.tar.gz
tar -czf /shoggoth/bringup/rendered/plugin.tar.gz -C "${PLUGIN_STAGE}" .
rm -rf "${PLUGIN_STAGE}"
chmod a+r /shoggoth/bringup/rendered/plugin.tar.gz

mkdir -p /shoggoth/data/gitea
chown 1000:1000 /shoggoth/data/gitea

mkdir -p /shoggoth/data/gitea-runner
chown 1000:1000 /shoggoth/data/gitea-runner

mkdir -p /shoggoth/data/redmine-database
chown 70:70 /shoggoth/data/redmine-database

mkdir -p /shoggoth/data/redmine-files /shoggoth/data/redmine-bundle-cache
chown 999:999 /shoggoth/data/redmine-files /shoggoth/data/redmine-bundle-cache

mkdir -p /shoggoth/data/litellm
chown 1000:1000 /shoggoth/data/litellm
