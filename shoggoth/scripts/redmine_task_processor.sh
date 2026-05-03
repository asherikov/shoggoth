#!/usr/bin/env bash

set -euxo pipefail

ENV_FILE="${SHOGGOTH_ENV:-/shoggoth/env}"
set -a
source "${ENV_FILE}"
set +a

strip_repo_to_project() {
    local repo="$1"
    repo="${repo#ssh://}"
    repo="${repo#git://}"
    repo="${repo#http://}"
    repo="${repo#https://}"
    repo="${repo#*@}"
    repo="${repo%.git}"
    if [[ "${repo}" == *:* ]]; then
        repo="${repo#*:}"
    fi
    echo "${repo}" | awk -F'/' '{print $NF}'
}

TASK_ID="${1:?Usage: redmine_task_processor.sh TASK_ID}"

TASK_DETAILS="$(redmine issues get "${TASK_ID}" --journals --children --output=json)"

TASK_SUBJECT="$(echo "${TASK_DETAILS}" | jq -r '.subject')"
TASK_PROJECT="$(echo "${TASK_DETAILS}" | jq -r '.project.name // empty')"
TASK_PROJECT_ID="$(echo "${TASK_DETAILS}" | jq -r '.project.id // empty')"

if [ -z "${TASK_PROJECT}" ]; then
    TASK_DESCRIPTION="$(echo "${TASK_DETAILS}" | jq -r '(.description // "")')"
    TASK_REPO="$(echo "${TASK_DESCRIPTION}" | grep -Eo '(https?|ssh)://[^ ]+|git@[^ ]+' | head -1)" || true
    if [ -n "${TASK_REPO}" ]; then
        TASK_PROJECT="$(strip_repo_to_project "${TASK_REPO}")"
    fi
fi

export SHOGGOTH_PROJECT="${TASK_PROJECT}"
export SHOGGOTH_PROJECT_ID="${TASK_PROJECT_ID}"

qwen --yolo --output-format json --prompt "Find and execute Redmine task #${TASK_ID}: ${TASK_SUBJECT}

Task details:
${TASK_DETAILS}"

redmine issues update "${TASK_ID}" --status "Resolved"