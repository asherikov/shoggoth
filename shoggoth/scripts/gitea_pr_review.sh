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
export SHOGGOTH_PROJECT="$(strip_repo_to_project "${PR_REPO}")"

cd /ccws/workspace/src

REPO_DIR="$(echo "${PR_REPO}" | cut -d'/' -f2)"

if [ ! -d "${REPO_DIR}" ]; then
    tea repos list --owner "$(echo "${PR_REPO}" | cut -d'/' -f1)" -o json | jq -r ".[].ssh_url" | head -1 | xargs git clone
fi

cd "${REPO_DIR}"
git fetch origin
git checkout "origin/${PR_BRANCH}"

qwen --yolo --prompt "/review ${PR_URL}"