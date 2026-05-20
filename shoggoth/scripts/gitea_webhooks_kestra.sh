#!/usr/bin/env bash

set -euxo pipefail

ENV_FILE="${SHOGGOTH_ENV:-/shoggoth/env}"
set -a
source "${ENV_FILE}"
set +a

GITEA_API="${GITEA_SERVER_URL:-http://${GITEA_URL:-git.shoggoth.local}}/api/v1"
GITEA_AUTH_TOKEN="${GITEA_SERVER_TOKEN:-${GITEA_TOKEN}}"
KESTRA_HOST="${KESTRA_HOST:-kestra.shoggoth.local}"

if [ $# -ge 1 ]; then
    GITEA_PROJECTS="${*}"
else
    mapfile -t GITEA_PROJECTS < <(curl -s \
        "${GITEA_API}/orgs?page=1&limit=50" \
        -H "accept: application/json" \
        -H "Authorization: token ${GITEA_AUTH_TOKEN}" \
        | jq -r '.[].username')
fi

if [ ${#GITEA_PROJECTS[@]} -eq 0 ]; then
    echo "No Gitea projects found"
    exit 0
fi

for GITEA_PROJECT in "${GITEA_PROJECTS[@]}"; do
    EXISTING="$(curl -s \
        "${GITEA_API}/orgs/${GITEA_PROJECT}/hooks" \
        -H "accept: application/json" \
        -H "Authorization: token ${GITEA_AUTH_TOKEN}")"

    WEBHOOKS=("
        http://${KESTRA_HOST}/api/v1/main/executions/webhook/shoggoth/gitea-pr-review/key|pull_request
        http://${KESTRA_HOST}/api/v1/main/executions/webhook/shoggoth/gitea-pr-comment/key|pull_request_review,pull_request_comment
        http://${KESTRA_HOST}/api/v1/main/executions/webhook/shoggoth/gitea-ci-failure/key|workflow_run
    ")

    mapfile -t HOOK_IDS < <(echo "${EXISTING}" | jq -r '.[].id')
    for HOOK_ID in "${HOOK_IDS[@]}"; do
        echo "Removing webhook from ${GITEA_PROJECT} (id=${HOOK_ID})"
        curl -s -X DELETE \
            "${GITEA_API}/orgs/${GITEA_PROJECT}/hooks/${HOOK_ID}" \
            -H "Authorization: token ${GITEA_AUTH_TOKEN}" > /dev/null
    done

    for WEBHOOK_SPEC in ${WEBHOOKS[@]}; do
        WEBHOOK_URL="${WEBHOOK_SPEC%%|*}"
        WEBHOOK_EVENTS="${WEBHOOK_SPEC##*|}"

        EVENTS_JSON="$(echo "${WEBHOOK_EVENTS}" | sed 's/,/","/g' | sed 's/^/"/;s/$/"/')"

        echo "Adding webhook to ${GITEA_PROJECT}: ${WEBHOOK_URL}"

        curl -s -X POST \
            "${GITEA_API}/orgs/${GITEA_PROJECT}/hooks" \
            -H "accept: application/json" \
            -H "Authorization: token ${GITEA_AUTH_TOKEN}" \
            -H "Content-Type: application/json" \
            -d "{ \
                \"active\": true, \
                \"config\": { \
                    \"content_type\": \"json\", \
                    \"url\": \"${WEBHOOK_URL}\" \
                }, \
                \"events\": [${EVENTS_JSON}], \
                \"type\": \"gitea\" \
            }"
    done
done