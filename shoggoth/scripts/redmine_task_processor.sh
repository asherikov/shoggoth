#!/usr/bin/env bash

set -euxo pipefail

ENV_FILE="${SHOGGOTH_ENV:-/shoggoth/env}"
set -a
source "${ENV_FILE}"
set +a

normalize_for_branch() {
    local INPUT="$1"
    echo "${INPUT}" \
        | tr '[:upper:]' '[:lower:]' \
        | sed 's/[^a-z0-9]/-/g' \
        | sed 's/-\+/-/g' \
        | sed 's/^-\|-$//g'
}

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

if [ -z "${TASK_PROJECT}" ]; then
    TASK_DESCRIPTION="$(echo "${TASK_DETAILS}" | jq -r '(.description // "")')"
    TASK_REPO="$(echo "${TASK_DESCRIPTION}" | grep -Eo '(https?|ssh)://[^ ]+|git@[^ ]+' | head -1)" || true
    if [ -n "${TASK_REPO}" ]; then
        TASK_PROJECT="$(strip_repo_to_project "${TASK_REPO}")"
    fi
fi

export SHOGGOTH_PROJECT="$(normalize_for_branch "${TASK_PROJECT}")"
export SHOGGOTH_BRANCH="${SHOGGOTH_PROJECT}/$(normalize_for_branch "${TASK_SUBJECT}")"

qwen --yolo --output-format json --prompt "Find and execute Redmine task #${TASK_ID}: ${TASK_SUBJECT}

Task details:
${TASK_DETAILS}

If the given task requires modification of repositories, create a feature branch with the name taken from the SHOGGOTH_BRANCH environment variable (${SHOGGOTH_BRANCH}) before making changes, push it after completion, and open a pull request using the Gitea CLI (tea)."

redmine issues update "${TASK_ID}" --status "Resolved"