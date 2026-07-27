#!/usr/bin/env bash
set -euo pipefail

. /shoggoth/bringup/rendered/cdash/cdash.env

export APP_KEY DB_PASSWORD
export CDASH_AUTHENTICATION_PROVIDER LOGIN_FIELD
export LDAP_HOSTS LDAP_PORT LDAP_PROVIDER LDAP_LOCATE_USERS_BY LDAP_USERNAME LDAP_PASSWORD LDAP_BASE_DN

cd /cdash

for i in $(seq 1 60); do
    if php artisan migrate:status > /dev/null 2>&1; then
        echo "shoggoth: CDash is ready"
        break
    fi
    echo "shoggoth: Waiting for CDash to be ready (attempt ${i}/60)"
    sleep 2
done

if ! php artisan migrate:status > /dev/null 2>&1; then
    echo "shoggoth: FATAL: CDash never became ready" >&2
    exit 1
fi

php artisan user:save \
    --email=sslave \
    --firstname=Slave \
    --lastname=User \
    --institution=shoggoth \
    --password="${OPENLDAP_S_SLAVE_PASSWORD}" \
    --admin=0 \
    || echo "shoggoth: WARNING: user:save failed (may already exist or DB issue)" >&2

CDASH_URL="http://localhost"
COOKIE_JAR="$(mktemp)"
trap 'rm -f "${COOKIE_JAR}"' EXIT

echo "shoggoth: Waiting for CDash web server"
for i in $(seq 1 60); do
    if curl -s -o /dev/null -w '%{http_code}' "${CDASH_URL}/login" 2>/dev/null | grep -q '^200'; then
        echo "shoggoth: CDash web server is ready"
        break
    fi
    if [ "${i}" -eq 60 ]; then
        echo "shoggoth: FATAL: CDash web server never became ready" >&2
        exit 1
    fi
    sleep 2
done

CSRF_PAGE="$(curl -s -c "${COOKIE_JAR}" "${CDASH_URL}/login" 2>/dev/null)" || {
    echo "shoggoth: FATAL: Could not fetch CDash login page" >&2
    exit 1
}
CSRF_TOKEN="$(printf '%s' "${CSRF_PAGE}" \
    | sed -n 's/.*name="_token" value="\([^"]*\)".*/\1/p' | head -1)"

if [ -z "${CSRF_TOKEN}" ]; then
    echo "shoggoth: FATAL: Could not get CSRF token from CDash login page" >&2
    exit 1
fi

LOGIN_RESULT="$(curl -s -b "${COOKIE_JAR}" -c "${COOKIE_JAR}" \
    -X POST "${CDASH_URL}/login" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "_token=${CSRF_TOKEN}&email=sslave&password=${OPENLDAP_S_SLAVE_PASSWORD}" \
    -o /dev/null -w '%{http_code} %{redirect_url}')"
LOGIN_CODE="${LOGIN_RESULT%% *}"
LOGIN_REDIRECT="${LOGIN_RESULT#* }"
if [ "${LOGIN_CODE}" = "302" ] && printf '%s' "${LOGIN_REDIRECT}" | grep -qE '^https?://[^/]+/login'; then
    echo "shoggoth: FATAL: CDash login failed for sslave (redirected back to login)" >&2
    echo "shoggoth: Check /cdash/storage/logs/laravel.log for LDAP auth errors" >&2
    exit 1
fi
if [ "${LOGIN_CODE}" != "200" ] && [ "${LOGIN_CODE}" != "302" ]; then
    echo "shoggoth: FATAL: CDash login failed for sslave (HTTP ${LOGIN_CODE})" >&2
    exit 1
fi

GRAPHQL_RESPONSE="$(curl -s -b "${COOKIE_JAR}" \
    -X POST "${CDASH_URL}/graphql" \
    -H "Content-Type: application/json" \
    -H "X-Requested-With: XMLHttpRequest" \
    -d '{
        "query": "query { authenticationTokens(first: 100000) { edges { node { id description } } } }"
    }')" || {
    echo "shoggoth: FATAL: GraphQL query for existing tokens failed" >&2
    exit 1
}

OLD_TOKEN_IDS="$(printf '%s' "${GRAPHQL_RESPONSE}" \
    | php -r '$d=json_decode(stream_get_contents(STDIN),true);foreach(($d["data"]["authenticationTokens"]["edges"]??[]) as $e){if(($e["node"]["description"]??"")==="shoggoth-slave"){echo $e["node"]["id"]."\n";}}')"

for TOKEN_ID in ${OLD_TOKEN_IDS}; do
    curl -s -b "${COOKIE_JAR}" \
        -X POST "${CDASH_URL}/graphql" \
        -H "Content-Type: application/json" \
        -H "X-Requested-With: XMLHttpRequest" \
        -d "{\"query\": \"mutation DeleteToken(\$input: DeleteAuthenticationTokenInput!) { deleteAuthenticationToken(input: \$input) { message } }\", \"variables\": {\"input\": {\"tokenId\": ${TOKEN_ID}}}}" \
        > /dev/null 2>&1 || true
done

GRAPHQL_RESPONSE="$(curl -s -b "${COOKIE_JAR}" \
    -X POST "${CDASH_URL}/graphql" \
    -H "Content-Type: application/json" \
    -H "X-Requested-With: XMLHttpRequest" \
    -d '{
        "query": "mutation CreateToken($input: CreateAuthenticationTokenInput!) { createAuthenticationToken(input: $input) { rawToken } }",
        "variables": {
            "input": {
                "scope": "FULL_ACCESS",
                "description": "shoggoth-slave",
                "expiration": "2999-12-31T23:59:59Z"
            }
        }
    }')" || {
    echo "shoggoth: FATAL: GraphQL createAuthenticationToken request failed" >&2
    exit 1
}

RAW_TOKEN="$(printf '%s' "${GRAPHQL_RESPONSE}" \
    | php -r '$d=json_decode(stream_get_contents(STDIN),true);echo $d["data"]["createAuthenticationToken"]["rawToken"]??"";')"

if [ -z "${RAW_TOKEN}" ]; then
    echo "shoggoth: FATAL: GraphQL did not return a raw token" >&2
    printf '%s\n' "${GRAPHQL_RESPONSE}" >&2
    exit 1
fi

curl -s -o /dev/null -w '%{http_code}' -X POST "${OPENBAO_ADDR}/v1/secret/data/cdash/api-token" \
    -H "X-Vault-Token: ${SHOGGOTH_VAULT_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"data\":{\"value\":\"${RAW_TOKEN}\"}}" | grep -qE '^(200|204)' || {
    echo "shoggoth: FATAL: Failed to store CDash API token in OpenBao" >&2
    exit 1
}

echo "shoggoth: CDash slave token created and stored in OpenBao"
echo "shoggoth: Restart web-internal to pick up the new token"