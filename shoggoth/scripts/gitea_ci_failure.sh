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

CONCLUSION="$(echo "${PAYLOAD}" | jq -r '.workflow_run.conclusion')"
if [ "${CONCLUSION}" != "failure" ]; then
    echo "Ignoring workflow_run conclusion: ${CONCLUSION}"
    exit 0
fi

CI_REPO="$(echo "${PAYLOAD}" | jq -r '.repository.full_name')"
CI_SHA="$(echo "${PAYLOAD}" | jq -r '.workflow_run.head_sha')"
CI_BRANCH="$(echo "${PAYLOAD}" | jq -r '.workflow_run.head_branch')"
CI_RUN_URL="$(echo "${PAYLOAD}" | jq -r '.workflow_run.html_url')"
CI_WORKFLOW="$(echo "${PAYLOAD}" | jq -r '.workflow.name')"

export SHOGGOTH_REPO="${CI_REPO}"
if [ -z "${SHOGGOTH_PROJECT:-}" ]; then
    export SHOGGOTH_PROJECT="$(resolve_project "${CI_BRANCH}" "$(echo "${CI_REPO}" | cut -d'/' -f2)")"
fi

cd /ccws/workspace/src

REPO_DIR="$(echo "${CI_REPO}" | cut -d'/' -f2)"

if [ ! -d "${REPO_DIR}" ]; then
    tea repos list --owner "$(echo "${CI_REPO}" | cut -d'/' -f1)" -o json | jq -r ".[].ssh_url" | head -1 | xargs git clone
fi

cd "${REPO_DIR}"
git fetch origin
git checkout "${CI_SHA}"

CI_LOGS="$(tea actions runs view --repo "${CI_REPO}" --log 2>/dev/null || echo 'CI logs unavailable')"

qwen --yolo --prompt "CI workflow '${CI_WORKFLOW}' failed on repository ${CI_REPO} at commit ${CI_SHA} (branch ${CI_BRANCH}).
Run URL: ${CI_RUN_URL}

CI logs:
${CI_LOGS}

Fetch the CI log and try to resolve the issue. Fix the code, commit, and push."