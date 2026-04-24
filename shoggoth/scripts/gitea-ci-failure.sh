#!/usr/bin/env bash

set -euxo pipefail

ENV_FILE="${SHOGGOTH_ENV:-/shoggoth/env}"
set -a
source "${ENV_FILE}"
set +a

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