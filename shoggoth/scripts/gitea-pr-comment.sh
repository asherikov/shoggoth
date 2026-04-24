#!/usr/bin/env bash

set -euxo pipefail

ENV_FILE="${SHOGGOTH_ENV:-/shoggoth/env}"
set -a
source "${ENV_FILE}"
set +a

PAYLOAD="${GITEA_PAYLOAD}"

ACTION="$(echo "${PAYLOAD}" | jq -r '.action')"
if [ "${ACTION}" = "deleted" ]; then
    exit 0
fi

PR_NUMBER="$(echo "${PAYLOAD}" | jq -r '.pull_request.number')"
PR_URL="$(echo "${PAYLOAD}" | jq -r '.pull_request.html_url')"
PR_REPO="$(echo "${PAYLOAD}" | jq -r '.repository.full_name')"
PR_BRANCH="$(echo "${PAYLOAD}" | jq -r '.pull_request.head.ref')"
CLONE_URL="$(echo "${PAYLOAD}" | jq -r '.repository.ssh_url')"

mkdir -p /ccws/workspace/src
cd /ccws/workspace/src

REPO_DIR="$(echo "${PR_REPO}" | cut -d'/' -f2)"

if [ ! -d "${REPO_DIR}" ]; then
    git clone "${CLONE_URL}" "${REPO_DIR}"
fi

cd "${REPO_DIR}"
git fetch origin
git checkout "origin/${PR_BRANCH}"

qwen --yolo --prompt "Address the review comments on PR ${PR_URL}. Read the PR review comments, fix the code accordingly, commit, and push."
