#!/usr/bin/env bash
set -euo pipefail

OPENBAO_ADDR="${OPENBAO_ADDR:-http://openbao:80}"

bao_get_value() {
    local path="${1}"
    local tmp response http_code
    tmp="$(mktemp)"
    http_code="$(curl -sL -w '%{http_code}' -o "${tmp}" \
        -H "X-Vault-Token: ${SHOGGOTH_VAULT_TOKEN}" \
        "${OPENBAO_ADDR}/v1/secret/data/${path}" 2>/dev/null || echo "000")"
    response="$(cat "${tmp}" 2>/dev/null || true)"
    rm -f "${tmp}"
    if [ "${http_code}" = "000" ]; then
        echo "shoggoth: ERROR: OpenBao API unreachable for secret/data/${path}" >&2
        return 1
    fi
    if [ "${http_code}" = "404" ]; then
        return 0
    fi
    if [ "${http_code}" != "200" ]; then
        echo "shoggoth: ERROR: OpenBao returned HTTP ${http_code} for secret/data/${path}" >&2
        return 1
    fi
    printf '%s' "${response}" | jq -r '.data.data.value // empty'
}

bao_put() {
    local path="${1}"
    local key="${2}"
    local value="${3}"
    local payload http_code
    payload="$(printf '%s' "${value}" | jq -Rs . | jq -c --arg k "${key}" '{"data": {($k): .}}')"
    http_code="$(curl -sL -w '%{http_code}' -o /dev/null \
        -H "X-Vault-Token: ${SHOGGOTH_VAULT_TOKEN}" -H "Content-Type: application/json" \
        -X POST \
        -d "${payload}" \
        "${OPENBAO_ADDR}/v1/secret/data/${path}" 2>/dev/null || echo "000")"
    if [ "${http_code}" != "200" ] && [ "${http_code}" != "204" ]; then
        echo "shoggoth: ERROR: OpenBao PUT failed with HTTP ${http_code} for secret/data/${path}" >&2
        return 1
    fi
}

_bao_generate() {
    local style="${1}"
    local length="${2:-32}"
    if [ "${style}" = "hex" ]; then
        openssl rand -hex "${length}"
    else
        openssl rand -base64 "${length}" | tr -dc 'a-zA-Z0-9' | head -c "${length}"
    fi
}

bao_get_or_generate() {
    local path="${1}"
    local length="${2:-32}"
    local value
    value="$(bao_get_value "${path}")"
    if [ -n "${value}" ]; then
        printf '%s' "${value}"
        return
    fi
    value="$(_bao_generate base64 "${length}")"
    bao_put "${path}" "value" "${value}"
    printf '%s' "${value}"
}

bao_get_or_generate_hex() {
    local path="${1}"
    local length="${2:-24}"
    local value
    value="$(bao_get_value "${path}")"
    if [ -n "${value}" ]; then
        printf '%s' "${value}"
        return
    fi
    value="$(_bao_generate hex "${length}")"
    bao_put "${path}" "value" "${value}"
    printf '%s' "${value}"
}