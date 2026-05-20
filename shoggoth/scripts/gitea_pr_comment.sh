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
if [ "${ACTION}" = "deleted" ]; then
    exit 0
fi

PR_NUMBER="$(echo "${PAYLOAD}" | jq -r '.pull_request.number')"
PR_URL="$(echo "${PAYLOAD}" | jq -r '.pull_request.html_url')"
PR_REPO="$(echo "${PAYLOAD}" | jq -r '.repository.full_name')"
PR_BRANCH="$(echo "${PAYLOAD}" | jq -r '.pull_request.head.ref')"
CLONE_URL="$(echo "${PAYLOAD}" | jq -r '.repository.ssh_url')"

export SHOGGOTH_REPO="${PR_REPO}"
if [ -z "${SHOGGOTH_PROJECT:-}" ]; then
    export SHOGGOTH_PROJECT="$(resolve_project "${PR_BRANCH}" "$(echo "${PR_REPO}" | cut -d'/' -f2)")"
fi

mkdir -p /ccws/workspace/src
cd /ccws/workspace/src

REPO_DIR="$(echo "${PR_REPO}" | cut -d'/' -f2)"

if [ ! -d "${REPO_DIR}" ]; then
    git clone "${CLONE_URL}" "${REPO_DIR}"
fi

cd "${REPO_DIR}"
git fetch origin
git checkout "origin/${PR_BRANCH}"

tea login add --name shoggoth --url "${GITEA_SERVER_URL}" --token "${GITEA_SERVER_TOKEN}"
tea login default shoggoth

COMMENTS_JSON="$(tea pulls review-comments "${PR_NUMBER}" --repo "${PR_REPO}" --output json | jq -c '[.[] | select(.resolver == "") | {id: .id, path: .path, line: .line, body: .body}]')"

COMMENT_COUNT="$(echo "${COMMENTS_JSON}" | jq 'length')"
if [ "${COMMENT_COUNT}" -eq 0 ]; then
    echo "No unresolved review comments"
    exit 0
fi

SESSION_ID="$(cat /proc/sys/kernel/random/uuid)"

for I in $(seq 0 $((COMMENT_COUNT - 1))); do
    COMMENT_BODY="$(echo "${COMMENTS_JSON}" | jq -r ".[${I}].body")"
    COMMENT_PATH="$(echo "${COMMENTS_JSON}" | jq -r ".[${I}].path")"
    COMMENT_LINE="$(echo "${COMMENTS_JSON}" | jq -r ".[${I}].line")"

    if [ "${I}" -eq 0 ]; then
        qwen --yolo --session-id "${SESSION_ID}" --prompt "Address the following review comment on PR ${PR_URL} (file: ${COMMENT_PATH}, line: ${COMMENT_LINE}): ${COMMENT_BODY}."
    else
        qwen --yolo --resume "${SESSION_ID}" --prompt "Address the following review comment on PR ${PR_URL} (file: ${COMMENT_PATH}, line: ${COMMENT_LINE}): ${COMMENT_BODY}."
    fi
done

qwen --yolo --resume "${SESSION_ID}" --prompt "Commit any remaining changes and push."

for I in $(seq 0 $((COMMENT_COUNT - 1))); do
    COMMENT_ID="$(echo "${COMMENTS_JSON}" | jq -r ".[${I}].id")"
    tea pulls resolve --repo "${PR_REPO}" "${COMMENT_ID}" || true
done
