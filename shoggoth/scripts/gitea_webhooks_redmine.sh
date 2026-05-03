#!/usr/bin/env bash

set -euxo pipefail

ENV_FILE="${SHOGGOTH_ENV:-/shoggoth/env}"
set -a
source "${ENV_FILE}"
set +a

GITEA_API="${GITEA_SERVER_URL:-http://${GITEA_URL:-git.shoggoth.local}}/api/v1"
GITEA_AUTH_TOKEN="${GITEA_SERVER_TOKEN:-${GITEA_TOKEN}}"
REDMINE_HOST="$(echo "${REDMINE_SERVER:-http://redmine.shoggoth.local}" | sed 's|.*://||')"
REDMINE_WEBHOOK_SECRET="${REDMINE_WEBHOOK_SECRET:-}"

REDMINE_WEBHOOK_URL="http://${REDMINE_HOST}/forgejo/webhook"

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

    HOOK_ID="$(echo "${EXISTING}" | jq -r ".[] | select(.config.url | startswith(\"http://${REDMINE_HOST}/forgejo/webhook\")) | .id" | head -1)"

    if [ -n "${HOOK_ID}" ]; then
        echo "Replacing Redmine webhook in ${GITEA_PROJECT} (id=${HOOK_ID})"
        curl -s -X DELETE \
            "${GITEA_API}/orgs/${GITEA_PROJECT}/hooks/${HOOK_ID}" \
            -H "Authorization: token ${GITEA_AUTH_TOKEN}" > /dev/null
    else
        echo "Adding Redmine webhook to ${GITEA_PROJECT}"
    fi

    if [ -n "${REDMINE_WEBHOOK_SECRET}" ]; then
        curl -s -X POST \
            "${GITEA_API}/orgs/${GITEA_PROJECT}/hooks" \
            -H "accept: application/json" \
            -H "Authorization: token ${GITEA_AUTH_TOKEN}" \
            -H "Content-Type: application/json" \
            -d "{ \
                \"active\": true, \
                \"config\": { \
                    \"content_type\": \"json\", \
                    \"url\": \"${REDMINE_WEBHOOK_URL}\", \
                    \"secret\": \"${REDMINE_WEBHOOK_SECRET}\" \
                }, \
                \"events\": [\"push\", \"pull_request\", \"issues\"], \
                \"type\": \"gitea\" \
            }"
    else
        curl -s -X POST \
            "${GITEA_API}/orgs/${GITEA_PROJECT}/hooks" \
            -H "accept: application/json" \
            -H "Authorization: token ${GITEA_AUTH_TOKEN}" \
            -H "Content-Type: application/json" \
            -d "{ \
                \"active\": true, \
                \"config\": { \
                    \"content_type\": \"json\", \
                    \"url\": \"${REDMINE_WEBHOOK_URL}\" \
                }, \
                \"events\": [\"push\", \"pull_request\", \"issues\"], \
                \"type\": \"gitea\" \
            }"
    fi
done