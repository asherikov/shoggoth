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

resolve_project() {
    local BRANCH="$1"
    local REPO="$2"
    local BRANCH_PREFIX=""
    if echo "${BRANCH}" | grep -q '/'; then
        BRANCH_PREFIX="$(echo "${BRANCH}" | cut -d'/' -f1)"
        LOCAL_NORMALIZED="$(normalize_for_branch "${BRANCH_PREFIX}")"
        REDMINE_PROJECT="$(redmine projects list --output=json 2>/dev/null | jq -r --arg norm "${LOCAL_NORMALIZED}" '.[] | select(.identifier == $norm or (.name | ascii_downcase | gsub("[^a-z0-9]";"-") | gsub("-+";"-") | gsub("^-|-$";"")) == $norm) | .identifier' | head -1)" || true
        if [ -n "${REDMINE_PROJECT}" ]; then
            echo "${REDMINE_PROJECT}"
            return
        fi
    fi
    echo "${REPO}" | sed 's/\.git$//'
}

PAYLOAD="${GITEA_PAYLOAD}"

ACTION="$(echo "${PAYLOAD}" | jq -r '.action')"
if [ "${ACTION}" != "opened" ]; then
    echo "Ignoring pull_request action: ${ACTION}"
    exit 0
fi

PR_NUMBER="$(echo "${PAYLOAD}" | jq -r '.number')"
PR_URL="$(echo "${PAYLOAD}" | jq -r '.pull_request.html_url')"
PR_REPO="$(echo "${PAYLOAD}" | jq -r '.repository.full_name')"
PR_BRANCH="$(echo "${PAYLOAD}" | jq -r '.pull_request.head.ref')"

export SHOGGOTH_REPO="${PR_REPO}"
if [ -z "${SHOGGOTH_PROJECT:-}" ]; then
    export SHOGGOTH_PROJECT="$(resolve_project "${PR_BRANCH}" "$(echo "${PR_REPO}" | cut -d'/' -f2)")"
fi

cd /ccws/workspace/src

REPO_DIR="$(echo "${PR_REPO}" | cut -d'/' -f2)"

if [ ! -d "${REPO_DIR}" ]; then
    tea repos list --owner "$(echo "${PR_REPO}" | cut -d'/' -f1)" -o json | jq -r ".[].ssh_url" | head -1 | xargs git clone
fi

cd "${REPO_DIR}"
git fetch origin
git checkout "origin/${PR_BRANCH}"

qwen --yolo --prompt "/review ${PR_URL}"